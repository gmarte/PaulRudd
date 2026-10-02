"""
Provider-neutral LLM calls: a prompt plan in, the raw JSON reply out.

A PromptPlan is laid out from most to least stable, which is what prompt caching
rewards:

  1. static_rules — Paul's review rules (change only with a Paul release)
  2. repo_context — the repo's guidelines and instructions (change on merges to base)
  3. pr_context   — the PR's description, file list and diff (change on every push)
  4. task         — what this one call should do (changes on every call)

A transport (llm_anthropic, llm_litellm) turns the plan into a provider request.
It raises Retry for errors worth retrying; this module retries them with backoff,
within the run's time budget, and keeps the run's token and cost totals.
"""

import dataclasses
import random
import threading
import time
from dataclasses import dataclass

LLM_TIMEOUT_SECONDS = 300
_RETRY_DELAYS = (5, 10, 20, 40, 80)  # seconds before each retry; a retry-after header takes precedence
_MIN_SECONDS_FOR_A_CALL = 15

# USD per million tokens: (input, output, cache read). Writing to the cache costs
# 1.25x the input price for the 5-minute TTL and 2x for the 1-hour TTL.
_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

# Token and cost totals for the run, shown in the review details and the job summary.
USAGE = {
    "calls": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
    "output_tokens": 0, "cost_usd": 0.0, "cost_known": True,
}
_usage_lock = threading.Lock()

# time.monotonic() value after which no new LLM call or retry starts (None = no limit).
_deadline = None

# Files are reviewed on worker threads; each tags its log lines with its file.
_local = threading.local()


class ReviewError(Exception):
    """An LLM call that produced no usable review. `reason` is a key of coverage.FAIL_LABELS."""
    reason = "llm_rejected"


class LLMUnavailable(ReviewError):
    reason = "llm_unavailable"


class TruncatedOutput(ReviewError):
    reason = "truncated"


class InputTooLarge(ReviewError):
    reason = "context_exceeded"


class InvalidOutput(ReviewError):
    reason = "invalid_output"


class Refused(ReviewError):
    reason = "refused"


class OutOfTime(ReviewError):
    reason = "time_budget"


class OverBudget(ReviewError):
    reason = "budget"


class Retry(Exception):
    """Raised by a transport for an error worth retrying, with the provider's retry-after hint."""

    def __init__(self, error: Exception, retry_after: float | None = None):
        super().__init__(f"{type(error).__name__}: {error}")
        self.error = error
        self.retry_after = retry_after


@dataclass(frozen=True)
class PromptPlan:
    static_rules: str
    repo_context: str
    pr_context: str
    task: str = ""

    def with_task(self, task: str) -> "PromptPlan":
        return dataclasses.replace(self, task=task)


@dataclass
class Usage:
    fresh_input: int = 0    # input tokens neither read from nor written to the cache
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cost_usd: float | None = None


@dataclass
class Reply:
    text: str | None
    stop_reason: str        # "end" | "max_tokens" | "refusal"
    usage: Usage


# ── Calls ────────────────────────────────────────────────────────────────────

def complete(plan: PromptPlan, config: dict) -> str:
    """One LLM call with retries. Returns the reply text or raises a ReviewError."""
    transport = _transport(config)
    attempt = 0
    while True:
        attempt += 1
        left = seconds_left()
        if left is not None and left < _MIN_SECONDS_FOR_A_CALL:
            raise OutOfTime("time budget reached")
        max_cost = config["budget"]["max_cost_usd"]
        if USAGE["cost_known"] and USAGE["cost_usd"] >= max_cost:
            raise OverBudget(f"estimated cost reached budget.max_cost_usd (${max_cost:.2f})")
        timeout = LLM_TIMEOUT_SECONDS if left is None else min(LLM_TIMEOUT_SECONDS, left)
        try:
            reply = transport.send(plan, config, timeout)
        except Retry as e:
            if attempt > len(_RETRY_DELAYS):
                raise LLMUnavailable(f"{type(e.error).__name__} after {attempt} attempts") from e.error
            delay = min(e.retry_after, 120.0) if e.retry_after is not None else (
                _RETRY_DELAYS[attempt - 1] * random.uniform(0.8, 1.2))
            left = seconds_left()
            if left is not None and delay + _MIN_SECONDS_FOR_A_CALL > left:
                raise OutOfTime(f"{type(e.error).__name__}; no time left to retry") from e.error
            log(f"    {type(e.error).__name__} (attempt {attempt}); retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

        record_usage(reply.usage)
        if reply.stop_reason == "max_tokens":
            raise TruncatedOutput(f"response hit max_tokens ({config['max_tokens']})")
        if reply.stop_reason == "refusal" or not reply.text:
            raise Refused(f"no review content (stop reason: {reply.stop_reason})")
        return reply.text


def _transport(config: dict):
    # Imported on demand: LiteLLM is only installed when a non-Anthropic provider is configured.
    if config["provider"] == "anthropic":
        import llm_anthropic
        return llm_anthropic
    import llm_litellm
    return llm_litellm


# ── Logging ──────────────────────────────────────────────────────────────────

def set_log_prefix(prefix: str) -> None:
    """Tag this thread's log lines (for example with the file it is reviewing)."""
    _local.prefix = prefix


def log(message: str) -> None:
    print(f"{getattr(_local, 'prefix', '')}{message.lstrip()}" if getattr(_local, "prefix", "") else message)


# ── Time budget ──────────────────────────────────────────────────────────────

def set_deadline(deadline: float | None) -> None:
    """Stop starting LLM calls and retries after this time.monotonic() value."""
    global _deadline
    _deadline = deadline


def seconds_left() -> float | None:
    return None if _deadline is None else _deadline - time.monotonic()


# ── Usage and cost ───────────────────────────────────────────────────────────

def record_usage(usage: Usage) -> None:
    with _usage_lock:
        USAGE["calls"] += 1
        USAGE["input_tokens"] += usage.fresh_input + usage.cache_read + usage.cache_write
        USAGE["cache_read_tokens"] += usage.cache_read
        USAGE["cache_write_tokens"] += usage.cache_write
        USAGE["output_tokens"] += usage.output
        if usage.cost_usd is None:
            USAGE["cost_known"] = False
        else:
            USAGE["cost_usd"] += usage.cost_usd
    total = usage.fresh_input + usage.cache_read + usage.cache_write
    log(f"    tokens: {total:,} in ({usage.cache_read:,} read from cache, "
        f"{usage.cache_write:,} written to cache), {usage.output:,} out")


def reset_usage() -> None:
    with _usage_lock:
        USAGE.update({"calls": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                      "output_tokens": 0, "cost_usd": 0.0, "cost_known": True})


def anthropic_cost(model: str, usage: Usage, write_1h_tokens: int = 0) -> float | None:
    """The cost of one Claude call from Paul's price table, or None for a model it doesn't know."""
    prices = next((p for name, p in sorted(_PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(name)), None)
    if prices is None:
        return None
    input_price, output_price, read_price = prices
    write_5m_tokens = usage.cache_write - write_1h_tokens
    return (
        usage.fresh_input * input_price
        + usage.cache_read * read_price
        + write_5m_tokens * input_price * 1.25
        + write_1h_tokens * input_price * 2
        + usage.output * output_price
    ) / 1_000_000
