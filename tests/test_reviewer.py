"""Prompt layers, tasks, parsing, normalization and the gate."""

import json

import pytest

import llm as llm_core
import reviewer
from conftest import llm_response, review_json

CONFIG = {
    "provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 16000, "temperature": None,
    "effort": "medium", "repo_context": "", "custom_instructions": "", "language": "",
    "cache": {"ttl": "5m"}, "budget": {"max_cost_usd": 5.0},
}


def _config(**overrides):
    return {**CONFIG, **overrides}


# ── Prompt layers ────────────────────────────────────────────────────────────

def test_the_plan_layers_rules_then_repo_then_pr():
    plan = reviewer.build_plan(_config(repo_context="### CLAUDE.md\n\nUse services.", custom_instructions="Flag raw SQL."),
                               "<pr>...</pr>")
    assert plan.static_rules.startswith("You are Paul") and "## Precision Rules" in plan.static_rules
    assert plan.repo_context.index("Use services.") < plan.repo_context.index("Flag raw SQL.")
    assert plan.pr_context == "<pr>...</pr>" and plan.task == ""


def test_repo_context_is_empty_without_guidelines_or_instructions():
    assert reviewer.build_plan(_config(), "pr").repo_context == ""


def test_placeholder_text_inside_guidelines_is_left_alone():
    # B16: chained .replace() used to expand placeholders again inside the injected CLAUDE.md.
    plan = reviewer.build_plan(_config(repo_context="The {CUSTOM_INSTRUCTIONS} placeholder lives here.",
                                       custom_instructions="Flag raw SQL."), "pr")
    assert plan.repo_context.count("Flag raw SQL.") == 1
    assert "The {CUSTOM_INSTRUCTIONS} placeholder" in plan.repo_context


def test_language_is_a_setting_not_hard_coded_spanish():
    assert "Spanish" not in reviewer.build_plan(_config(), "pr").static_rules
    assert "in Spanish" in reviewer.build_plan(_config(language="Spanish"), "pr").repo_context


def test_pr_context_carries_title_description_files_and_diff():
    block = reviewer.pr_context("Add search", "x" * 5000, "M  a.py  +2 -0", "<diff>...</diff>")
    assert block.startswith("<pr>\nTitle: Add search")
    assert "[description truncated]" in block
    assert "<changed_files>\nM  a.py  +2 -0\n</changed_files>" in block and block.endswith("<diff>...</diff>")


def test_review_task_carries_label_prior_findings_file_and_patch():
    prior = [{"file": "a.py", "line_start": 3, "line_end": 4, "severity": "major", "title": "Off by one"}]
    task = reviewer.review_task("a.py (part 1 of 2)", '<file path="a.py" lines="9">...</file>', "  1 +x", prior)
    assert task.startswith("TASK: review\n\nReview only this file: a.py (part 1 of 2)")
    assert "- [major] lines 3-4: Off by one" in task
    assert '<file path="a.py"' in task and task.endswith("<diff_to_review>\n  1 +x\n</diff_to_review>")
    assert "<diff_to_review>" not in reviewer.review_task("a.py", None, None, [])


def test_walkthrough_task_brings_diffs_only_in_compact_mode():
    assert reviewer.walkthrough_task().startswith("TASK: walkthrough") and "<diff_to_review>" not in reviewer.walkthrough_task()

    class Omitted:
        path, additions, deletions = "big.py", 900, 0

    task = reviewer.walkthrough_task("--- a.py\n+x\n", [Omitted()])
    assert "<diff_to_review>\n--- a.py\n+x\n</diff_to_review>" in task and "- big.py (+900 -0)" in task


# ── Calls through the envelope ───────────────────────────────────────────────

def _plan():
    return reviewer.build_plan(_config(), "pr").with_task("TASK: review\n\nReview only this file: a.py")


def test_the_envelope_section_for_the_task_is_used(llm):
    envelope = {"task": "review", "walkthrough": None, "verification": None,
                "review": {"issues": [], "test_recommendations": [], "resolved_prior_findings": []}}
    llm.review = lambda label, plan: llm_response(json.dumps(envelope))
    assert reviewer.review_file("a.py", _plan(), _config())["issues"] == []


def test_models_without_structured_outputs_may_answer_with_the_section_alone(llm):
    llm.review = lambda label, plan: llm_response(review_json())
    assert reviewer.review_file("a.py", _plan(), _config())["issues"] == []


def test_an_envelope_without_the_tasks_section_is_unusable(llm):
    envelope = {"task": "review", "walkthrough": {"summary": "s", "changes": []}, "review": None, "verification": None}
    llm.review = lambda label, plan: llm_response(json.dumps(envelope))
    with pytest.raises(llm_core.InvalidOutput):
        reviewer.review_file("a.py", _plan(), _config())
    assert len(llm.calls) == 2  # asked once more before giving up


def test_unusable_output_is_requested_once_more(llm):
    replies = iter([llm_response("Sorry, no JSON here."), llm_response(review_json())])
    llm.review = lambda label, plan: next(replies)
    assert reviewer.review_file("a.py", _plan(), _config())["issues"] == []


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
    with pytest.raises(llm_core.InvalidOutput):
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


@pytest.mark.parametrize("issue", [
    "critical: SQL injection at line 12", {}, {"severity": "major"},
    {"severity": "minor", "title": ", ", "description": " "},   # seen in a live Sonnet 5.5 run
])
def test_issues_that_cannot_be_shown_ask_for_the_review_again(issue):
    with pytest.raises(llm_core.InvalidOutput):
        reviewer.normalize_file_review({"issues": [issue]}, "a.py")


def test_findings_are_normalized_with_evidence_and_fixes():
    data = {
        "issues": [
            {"severity": "Major", "file": "elsewhere.py", "line_start": "12", "title": "Bug",
             "evidence": "x = f(y)", "category": "security", "confidence": "high", "pre_existing": True,
             "suggestion": {"explanation": "Guard it.", "replacement": "x = f(y) if y else None"}},
            {"severity": "minor", "title": "Nit", "suggestion": "Rename it.", "line_start": True,
             "category": "vibes", "confidence": "certain"},
        ],
        "test_recommendations": None,
    }
    first, second = reviewer.normalize_file_review(data, "a.py")["issues"]
    assert first["file"] == "a.py"  # from the request, never the model
    assert (first["severity"], first["line_start"], first["line_end"]) == ("major", 12, 12)
    assert (first["category"], first["confidence"], first["pre_existing"]) == ("security", "high", True)
    assert first["suggestion"] == {"explanation": "Guard it.",
                                   "autofix": {"original": "x = f(y)", "replacement": "x = f(y) if y else None"}}
    assert second["suggestion"] == {"explanation": "Rename it.", "autofix": None}
    assert (second["line_start"], second["category"], second["confidence"]) == (None, "correctness", "medium")


def test_a_review_without_issues_field_is_invalid():
    with pytest.raises(llm_core.InvalidOutput):
        reviewer.normalize_file_review({"findings": []}, "a.py")


def test_walkthrough_does_not_require_overall_severity():
    # B23: a missing overall_severity used to crash Pass 1.
    assert reviewer._normalize_walkthrough({"summary": "Adds search.", "changes": None}) == {
        "summary": "Adds search.", "changes": [],
    }


@pytest.mark.parametrize("value, expected", [("12", 12), (0, None), (-3, None), (float("inf"), None), (True, None)])
def test_line_numbers_must_be_positive_integers(value, expected):
    assert reviewer._as_int(value) == expected


def test_lone_surrogates_are_dropped_from_model_text():
    assert reviewer._as_text("bad \ud83d text").encode("utf-8")


# ── Gate and keys ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("severity, complete, on_incomplete, expected", [
    ("critical", True, "fail", "block"),
    ("major", False, "neutral", "block"),     # a blocking finding wins over coverage
    ("minor", True, "fail", "pass"),
    ("suggestion", False, "fail", "fail"),    # never pass while a file went unreviewed
    ("suggestion", False, "neutral", "neutral"),
])
def test_the_gate(severity, complete, on_incomplete, expected):
    assert reviewer.determines_outcome(severity, "major", complete, on_incomplete) == expected


def test_the_generic_key_maps_to_the_providers_variable(monkeypatch):
    monkeypatch.setenv("PAUL_API_KEY", "sk-generic")
    assert reviewer.api_key_available(_config(provider="openai"))
    reviewer.set_api_key_env(_config(provider="openai"))
    assert reviewer.os.environ["OPENAI_API_KEY"] == "sk-generic"


def test_a_finding_without_a_usable_title_is_named_from_its_description():
    data = {"issues": [{"severity": "minor", "title": "—", "description": "The loop skips one-word names. More text."}]}
    assert reviewer.normalize_file_review(data, "a.py")["issues"][0]["title"] == "The loop skips one-word names."
