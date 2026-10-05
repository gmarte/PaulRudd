"""Provider-neutral calls: retries, time and cost budgets, usage and cost accounting."""

import pytest

import llm as llm_core
import llm_anthropic
from conftest import llm_response

CONFIG = {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 16000,
          "budget": {"max_cost_usd": 5.0}}
PLAN = llm_core.PromptPlan("rules", "repo", "pr", "task")


def _transport(monkeypatch, *replies):
    """A fake transport that returns or raises each reply in turn; returns the list of calls."""
    calls, queue = [], list(replies)

    def send(plan, config, timeout):
        calls.append(timeout)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm_anthropic, "send", send)
    return calls


def test_rate_limits_are_retried_after_the_providers_delay(monkeypatch):
    # B7: a 429 used to crash the run.
    sleeps = []
    monkeypatch.setattr(llm_core.time, "sleep", sleeps.append)
    _transport(monkeypatch, llm_core.Retry(Exception("429"), retry_after=7.0), llm_response('{"issues": []}'))
    assert llm_core.complete(PLAN, CONFIG) == '{"issues": []}'
    assert sleeps == [7.0]


def test_persistent_failures_raise_llm_unavailable_after_six_attempts(monkeypatch):
    calls = _transport(monkeypatch, *[llm_core.Retry(Exception("Overloaded"))] * 6)
    with pytest.raises(llm_core.LLMUnavailable):
        llm_core.complete(PLAN, CONFIG)
    assert len(calls) == 6


def test_once_the_provider_stays_down_later_calls_fail_at_once(monkeypatch):
    # Review finding: during an outage every file used to sit through the whole backoff.
    calls = _transport(monkeypatch, *[llm_core.Retry(Exception("Overloaded"))] * 6)
    with pytest.raises(llm_core.LLMUnavailable):
        llm_core.complete(PLAN, CONFIG)
    with pytest.raises(llm_core.LLMUnavailable, match="earlier in this run"):
        llm_core.complete(PLAN, CONFIG)
    assert len(calls) == 6


@pytest.mark.parametrize("reply, error", [
    (llm_response('{"issues": [', finish_reason="length"), llm_core.TruncatedOutput),
    (llm_response(None, finish_reason="content_filter"), llm_core.Refused),
    (llm_response(""), llm_core.Refused),
])
def test_truncated_and_refused_replies_raise(monkeypatch, reply, error):
    _transport(monkeypatch, reply)
    with pytest.raises(error):
        llm_core.complete(PLAN, CONFIG)


def test_a_full_context_window_is_too_large_input_not_a_bad_answer(monkeypatch):
    # The cut-off JSON used to be re-asked with the same input, then failed as unusable.
    _transport(monkeypatch, llm_core.Reply('{"issues": [', "context_window", llm_core.Usage(cost_usd=0.0)))
    with pytest.raises(llm_core.InputTooLarge):
        llm_core.complete(PLAN, CONFIG)


def test_no_call_starts_once_the_time_budget_is_spent(monkeypatch):
    calls = _transport(monkeypatch)
    llm_core.set_deadline(0.0)  # long past
    with pytest.raises(llm_core.OutOfTime):
        llm_core.complete(PLAN, CONFIG)
    assert calls == []


def test_calls_get_no_more_time_than_is_left(monkeypatch):
    calls = _transport(monkeypatch, llm_response("{}"))
    llm_core.set_deadline(llm_core.time.monotonic() + 100)
    llm_core.complete(PLAN, CONFIG)
    assert calls[0] <= 100


def test_retries_stop_when_the_time_budget_would_run_out(monkeypatch):
    _transport(monkeypatch, *[llm_core.Retry(Exception("Overloaded"))] * 6)
    llm_core.set_deadline(llm_core.time.monotonic() + 30)  # room for a retry or two, not the whole backoff
    with pytest.raises(llm_core.OutOfTime):
        llm_core.complete(PLAN, CONFIG)


def test_no_call_starts_once_the_cost_budget_is_spent(monkeypatch):
    calls = _transport(monkeypatch)
    llm_core.USAGE["cost_usd"] = 5.0
    with pytest.raises(llm_core.OverBudget):
        llm_core.complete(PLAN, CONFIG)
    assert calls == []


def test_usage_is_totalled_including_cache_reads(monkeypatch):
    _transport(monkeypatch, llm_response("{}", prompt_tokens=9000, cache_read=7500))
    llm_core.complete(PLAN, CONFIG)
    usage = llm_core.USAGE
    assert (usage["calls"], usage["input_tokens"], usage["cache_read_tokens"]) == (1, 9000, 7500)
    assert usage["cost_known"] and usage["cost_usd"] == pytest.approx(0.001)


def test_an_unknown_price_marks_the_total_cost_unknown():
    llm_core.record_usage(llm_core.Usage(fresh_input=10, cost_usd=None))
    assert llm_core.USAGE["cost_known"] is False


def test_an_unknown_price_is_reported_once(monkeypatch, capsys):
    _transport(monkeypatch, *[llm_core.Reply("{}", "end", llm_core.Usage(fresh_input=10, cost_usd=None))] * 2)
    llm_core.complete(PLAN, CONFIG)
    llm_core.complete(PLAN, CONFIG)
    assert capsys.readouterr().out.count("can't estimate this run's cost or enforce budget.max_cost_usd") == 1


def test_claude_costs_follow_the_price_table():
    usage = llm_core.Usage(fresh_input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, output=1_000_000)
    # Sonnet 5.5: $2 in, $0.20 cache read, 1.25x for 5-minute writes, $10 out.
    assert llm_core.anthropic_cost("claude-sonnet-5-5", usage) == pytest.approx(2 + 0.2 + 2.5 + 10)
    # 1-hour writes cost 2x input; dated model ids match their family.
    assert llm_core.anthropic_cost("claude-haiku-4-5-20251001", llm_core.Usage(cache_write=1_000_000),
                                   write_1h_tokens=1_000_000) == pytest.approx(2.0)
    assert llm_core.anthropic_cost("claude-unknown-9", usage) is None


def test_log_lines_carry_the_threads_prefix(capsys):
    llm_core.set_log_prefix("[a.py] ")
    llm_core.log("    tokens: 5 in")
    assert capsys.readouterr().out == "[a.py] tokens: 5 in\n"


def test_a_log_line_cannot_start_a_workflow_command(capsys):
    # Review finding: a provider message (or file path) with a line break could start "::warning::...".
    llm_core.log("    Not reviewed: bad request\n::warning::Security review passed")
    assert capsys.readouterr().out == "    Not reviewed: bad request ::warning::Security review passed\n"
