"""The LLM layer: request shape, retries, parsing, normalization, prompts and the gate."""

import httpx
import litellm
import pytest

import reviewer
from conftest import llm_response, review_json

CONFIG = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "max_tokens": 16000,
    "temperature": None,
    "repo_context": "",
    "custom_instructions": "",
    "language": "",
}


def _config(**overrides):
    return {**CONFIG, **overrides}


# ── Request shape ────────────────────────────────────────────────────────────

def test_system_prompt_is_a_system_message_cached_on_anthropic(llm):
    # B5: the old top-level `system=` kwarg was dropped by Gemini and rejected by OpenAI.
    reviewer.review_file("a.py", "Review only this file: a.py", _config())
    messages = llm.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Severity Model" in messages[0]["content"][0]["text"]
    assert messages[1] == {"role": "user", "content": "Review only this file: a.py"}
    assert "system" not in llm.calls[0]


def test_other_providers_get_a_plain_system_message(llm):
    # B19: the old default of 32,768 was more than gpt-4o can produce.
    reviewer.review_file("a.py", "Review only this file: a.py", _config(provider="openai", model="gpt-4o", max_tokens=32768))
    call = llm.calls[0]
    assert call["model"] == "openai/gpt-4o"
    assert isinstance(call["messages"][0]["content"], str)
    assert call["max_tokens"] == 16384  # capped at gpt-4o's output limit


@pytest.mark.parametrize("model, sent", [
    ("claude-sonnet-4-6", True),    # accepts temperature; the old regex never sent it
    ("claude-sonnet-5-5", False),   # rejects sampling parameters
    ("claude-opus-5-5", False),
    ("claude-opus-4-7", False),
])
def test_temperature_is_sent_only_to_models_that_accept_it(llm, model, sent):
    # B6
    reviewer.review_file("a.py", "Review only this file: a.py", _config(model=model, temperature=0))
    assert ("temperature" in llm.calls[0]) is sent


def test_temperature_is_not_sent_unless_configured(llm):
    reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert "temperature" not in llm.calls[0]


def test_a_model_that_rejects_temperature_is_retried_without_it(monkeypatch):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise litellm.exceptions.BadRequestError(
                message="temperature is not supported for this model", model="x", llm_provider="anthropic"
            )
        return llm_response(review_json())

    monkeypatch.setattr(reviewer.litellm, "completion", completion)
    reviewer.review_file("a.py", "Review only this file: a.py", _config(model="claude-new-9", temperature=0))
    assert ["temperature" in c for c in calls] == [True, False]


def test_every_call_has_a_timeout(llm):
    reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert llm.calls[0]["timeout"] == reviewer.LLM_TIMEOUT_SECONDS


@pytest.mark.parametrize("model, provider, expected", [
    ("claude-sonnet-4-6", "anthropic", "anthropic/claude-sonnet-4-6"),
    ("gemini-2.5-pro", "google", "gemini/gemini-2.5-pro"),
    ("gemini/gemini-2.5-pro", "google", "gemini/gemini-2.5-pro"),  # B19: used to become gemini/gemini/...
    ("google/gemini-2.5-pro", "google", "gemini/gemini-2.5-pro"),
    ("openai/gpt-4o", "openai", "openai/gpt-4o"),
])
def test_model_strings_resolve_to_litellm_prefixes(model, provider, expected):
    assert reviewer.resolve_model({"model": model, "provider": provider}) == expected


# ── Retries ──────────────────────────────────────────────────────────────────

def _rate_limited(retry_after):
    response = httpx.Response(429, headers={"retry-after": retry_after}, request=httpx.Request("POST", "https://x"))
    return litellm.exceptions.RateLimitError(message="slow down", llm_provider="anthropic", model="x", response=response)


def test_rate_limits_are_retried_after_the_providers_delay(monkeypatch):
    # B7: a 429 used to crash the run.
    sleeps, attempts = [], []
    monkeypatch.setattr(reviewer.time, "sleep", sleeps.append)

    def completion(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise _rate_limited("7")
        return llm_response(review_json())

    monkeypatch.setattr(reviewer.litellm, "completion", completion)
    reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert sleeps == [7.0]


@pytest.mark.parametrize("error", [
    litellm.exceptions.InternalServerError(message="Overloaded", llm_provider="anthropic", model="x"),
    litellm.exceptions.Timeout(message="timed out", model="x", llm_provider="anthropic"),
    litellm.exceptions.APIConnectionError(message="reset", llm_provider="anthropic", model="x"),
])
def test_persistent_failures_raise_llm_unavailable_after_six_attempts(monkeypatch, error):
    attempts = []

    def completion(**kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(reviewer.litellm, "completion", completion)
    with pytest.raises(reviewer.LLMUnavailable):
        reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert len(attempts) == 6


def test_truncated_and_refused_responses_raise(monkeypatch):
    monkeypatch.setattr(reviewer.litellm, "completion", lambda **kw: llm_response('{"issues": [', finish_reason="length"))
    with pytest.raises(reviewer.TruncatedOutput):
        reviewer.review_file("a.py", "Review only this file: a.py", _config())

    monkeypatch.setattr(reviewer.litellm, "completion", lambda **kw: llm_response(None, finish_reason="content_filter"))
    with pytest.raises(reviewer.Refused):
        reviewer.review_file("a.py", "Review only this file: a.py", _config())


def test_unusable_output_is_requested_once_more(monkeypatch):
    replies = iter([llm_response("Sorry, no JSON here."), llm_response(review_json())])
    monkeypatch.setattr(reviewer.litellm, "completion", lambda **kw: next(replies))
    assert reviewer.review_file("a.py", "Review only this file: a.py", _config())["issues"] == []


# ── Parsing and normalization ────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '{"issues": []}',
    '```json\n{"issues": []}\n```',
    'Here is the review: {"issues": []} Hope it helps!',
    'The {curly} words come first.\n{"issues": []}',
])
def test_json_is_found_around_prose_and_fences(raw):
    assert reviewer._extract_json(raw) == {"issues": []}


@pytest.mark.parametrize("raw", [
    "no json",
    "[1, 2]",
    '{"issues": [',
    'Template: {"issues": []}. Review: {"issues": [{"title": "Bug"}]}',  # ambiguous: two objects
])
def test_missing_broken_or_ambiguous_json_is_invalid_output(raw):
    with pytest.raises(reviewer.InvalidOutput):
        reviewer._extract_json(raw)


@pytest.mark.parametrize("value, expected", [
    ("critical", "critical"), ("Critical", "critical"), ("BLOCKER", "critical"), ("crítico", "critical"),
    ("🔴 critical", "critical"), ("critique", "critical"), ("[CRITICAL]", "critical"),
    ("mayor", "major"), ("majeur", "major"), ("high", "major"), ("Severity: high", "major"), ("medium", "major"),
    ("menor", "minor"), ("mineur", "minor"), ("low", "minor"),
    ("nitpick", "suggestion"), ("sugerencia", "suggestion"),
    # Unknown → critical, so an odd label can't slip under any threshold.
    (None, "critical"), ("", "critical"), ("???", "critical"),
])
def test_severities_are_normalized_and_unknown_ones_fail_closed(value, expected):
    assert reviewer.normalize_severity(value) == expected


@pytest.mark.parametrize("issue", ["critical: SQL injection at line 12", {}, {"severity": "major"}])
def test_issues_that_cannot_be_shown_ask_for_the_review_again(issue):
    with pytest.raises(reviewer.InvalidOutput):
        reviewer.normalize_file_review({"issues": [issue]}, "a.py")


@pytest.mark.parametrize("value, expected", [("12", 12), (0, None), (-3, None), (float("inf"), None), (True, None)])
def test_line_numbers_must_be_positive_integers(value, expected):
    assert reviewer._as_int(value) == expected


def test_lone_surrogates_are_dropped_from_model_text():
    assert reviewer._as_text("bad \ud83d text").encode("utf-8")


def test_file_review_output_is_normalized():
    data = {
        "issues": [
            {"severity": "Major", "file": "elsewhere.py", "line_start": "12", "title": "Bug", "suggestion": "Fix it."},
            {"severity": "minor", "title": "Nit", "suggestion": None, "line_start": True},
        ],
        "test_recommendations": None,
    }
    result = reviewer.normalize_file_review(data, "a.py")
    first, second = result["issues"]
    assert first["file"] == "a.py"  # from the request, never the model
    assert (first["severity"], first["line_start"], first["line_end"]) == ("major", 12, 12)
    assert first["suggestion"] == {"explanation": "Fix it.", "autofix": None}
    assert second["suggestion"] == {"explanation": "", "autofix": None} and second["line_start"] is None
    assert result["test_recommendations"] == [] and result["resolved_prior_findings"] == []


def test_a_review_without_issues_field_is_invalid():
    with pytest.raises(reviewer.InvalidOutput):
        reviewer.normalize_file_review({"findings": []}, "a.py")


def test_walkthrough_does_not_require_overall_severity():
    # B23: a missing overall_severity used to crash Pass 1.
    assert reviewer._normalize_walkthrough({"summary": "Adds search.", "changes": None}) == {
        "summary": "Adds search.", "changes": [],
    }


# ── Prompts ──────────────────────────────────────────────────────────────────

def test_custom_instructions_appear_once_even_when_context_mentions_the_placeholder():
    # B16: chained .replace() expanded placeholders again inside the injected CLAUDE.md.
    config = _config(
        repo_context="### CLAUDE.md\n\nThe {CUSTOM_INSTRUCTIONS} placeholder lives in the template.",
        custom_instructions="Flag raw SQL.",
    )
    prompt = reviewer._build_prompt(reviewer.ISSUES_PROMPT_PATH, config)
    assert prompt.count("Flag raw SQL.") == 1
    assert "The {CUSTOM_INSTRUCTIONS} placeholder" in prompt


def test_language_is_a_setting_not_hard_coded_spanish():
    # B17
    plain = reviewer._build_prompt(reviewer.ISSUES_PROMPT_PATH, _config())
    spanish = reviewer._build_prompt(reviewer.ISSUES_PROMPT_PATH, _config(language="Spanish"))
    assert "Atendido" not in plain and "Spanish" not in plain
    assert "in Spanish" in spanish


def test_file_review_input_carries_pr_context_and_prior_findings():
    pr_block = reviewer.pr_context("Add search", "Search by RNC.", "M  a.py  +2 -0")
    prior = [{"file": "a.py", "line_start": 3, "line_end": 4, "severity": "major", "title": "Off by one"}]
    content = reviewer.file_review_input(pr_block, "a.py", "     1 +x", prior)
    assert "<pr>\nTitle: Add search" in content
    assert "<changed_files>\nM  a.py  +2 -0" in content
    assert "- [major] lines 3-4: Off by one" in content
    assert content.endswith("Review only this file: a.py\n\n<diff>\n     1 +x\n</diff>")


# ── Gate ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("severity, complete, on_incomplete, expected", [
    ("critical", True, "fail", "block"),
    ("major", False, "neutral", "block"),     # a blocking finding wins over coverage
    ("minor", True, "fail", "pass"),
    ("suggestion", False, "fail", "fail"),    # never pass while a file went unreviewed
    ("suggestion", False, "neutral", "neutral"),
])
def test_the_gate(severity, complete, on_incomplete, expected):
    assert reviewer.determines_outcome(severity, "major", complete, on_incomplete) == expected


def test_no_call_starts_once_the_time_budget_is_spent(llm):
    reviewer.set_deadline(0.0)  # long past
    with pytest.raises(reviewer.OutOfTime):
        reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert llm.calls == []


def test_retries_stop_when_the_time_budget_would_run_out(monkeypatch):
    def completion(**kwargs):
        raise litellm.exceptions.InternalServerError(message="Overloaded", llm_provider="anthropic", model="x")

    monkeypatch.setattr(reviewer.litellm, "completion", completion)
    reviewer.set_deadline(reviewer.time.monotonic() + 30)  # room for a retry or two, not the whole backoff
    with pytest.raises(reviewer.OutOfTime):
        reviewer.review_file("a.py", "Review only this file: a.py", _config())


def test_usage_is_recorded_including_cache_reads(monkeypatch):
    monkeypatch.setattr(reviewer.litellm, "completion", lambda **kw: llm_response(review_json(), prompt_tokens=9000, cache_read=7500))
    reviewer.review_file("a.py", "Review only this file: a.py", _config())
    assert reviewer.USAGE["input_tokens"] == 9000 and reviewer.USAGE["cache_read_tokens"] == 7500
