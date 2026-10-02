"""
Calls the configured LLM via LiteLLM using a two-pass strategy:

  Pass 1 — Walkthrough prompt: summary + one line per changed file
  Pass 2 — Issues prompt: one LLM call per changed file (or per part of an oversized file)

Every call returns validated, normalized data or raises a ReviewError, so the
caller records the file as not reviewed instead of treating it as clean.
"""

import functools
import json
import os
import random
import re
import time
import unicodedata
from pathlib import Path

import litellm


WALKTHROUGH_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "walkthrough_prompt.md"
ISSUES_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "issues_prompt.md"

SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]

# Severity labels by the start of their first word, after lower-casing and
# removing accents and decoration ("🔴 Critical" → "critical"). This also covers
# translations such as critique, crítico, mayor/majeur, menor/mineur, sugerencia.
# Anything else counts as critical, so an unexpected label can never slip under
# the blocking threshold.
_SEVERITY_PREFIXES = (
    ("crit", "critical"), ("block", "critical"), ("bloq", "critical"),
    ("maj", "major"), ("mayor", "major"), ("high", "major"), ("alt", "major"), ("grave", "major"),
    ("medi", "major"), ("moder", "major"),
    ("min", "minor"), ("menor", "minor"), ("low", "minor"), ("baj", "minor"),
    ("sug", "suggestion"), ("nit", "suggestion"), ("info", "suggestion"),
)

LLM_TIMEOUT_SECONDS = 300
_RETRY_DELAYS = (5, 10, 20, 40, 80)  # seconds before each retry; a retry-after header takes precedence
_RETRYABLE = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.InternalServerError,  # includes Anthropic's 529 "overloaded"
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.BadGatewayError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
)

# Models that reject sampling parameters such as temperature with a 400.
_NO_SAMPLING_MODELS = re.compile(r"claude-(?:opus-4-[78]|opus-5|sonnet-5|fable|mythos)")

_LITELLM_PREFIXES = {"anthropic", "openai", "gemini", "vertex_ai", "azure", "bedrock", "cohere"}
_PROVIDER_PREFIX = {"anthropic": "anthropic", "openai": "openai", "google": "gemini"}
_API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}

_MAX_PR_BODY_CHARS = 4000

# Token totals for the run, shown in the comment's review details.
USAGE = {"calls": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0}

# time.monotonic() value after which no new LLM call or retry starts (None = no limit).
_deadline = None


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


# ── Public API ───────────────────────────────────────────────────────────────

def review_walkthrough(content: str, config: dict) -> dict:
    """Pass 1: a summary and a one-line description per file. Returns {summary, changes[]}."""
    system_prompt = _build_prompt(WALKTHROUGH_PROMPT_PATH, config)
    return _call_for_json(system_prompt, content, config, _normalize_walkthrough)


def review_file(file_path: str, content: str, config: dict) -> dict:
    """
    Pass 2: issues in one file's diff, or one part of it.
    Returns {issues[], test_recommendations[], resolved_prior_findings[]}.
    """
    system_prompt = _build_prompt(ISSUES_PROMPT_PATH, config)
    return _call_for_json(system_prompt, content, config, lambda data: normalize_file_review(data, file_path))


def pr_context(title: str, body: str, file_table: str) -> str:
    """The PR-wide part of every user message: what the PR says it does, and what it touches."""
    body = (body or "").strip() or "(none)"
    if len(body) > _MAX_PR_BODY_CHARS:
        body = body[:_MAX_PR_BODY_CHARS] + "\n[description truncated]"
    return (
        f"<pr>\nTitle: {title or '(none)'}\nDescription:\n{body}\n</pr>\n\n"
        f"<changed_files>\n{file_table}\n</changed_files>"
    )


def walkthrough_input(pr_block: str, diff_text: str, omitted: list) -> str:
    parts = [pr_block, f"<diff>\n{diff_text}</diff>"]
    if omitted:
        names = "\n".join(f"- {c.path} (+{c.additions} -{c.deletions})" for c in omitted)
        parts.append(f"Diffs left out of this view to fit the size budget (describe them from their names only):\n{names}")
    return "\n\n".join(parts)


def file_review_input(pr_block: str, label: str, annotated_patch: str, prior_findings: list) -> str:
    parts = [pr_block]
    if prior_findings:
        lines = "\n".join(f"- [{f['severity']}] {_line_ref(f)}: {f['title']}" for f in prior_findings)
        parts.append(f"<prior_findings>\nReported by Paul on an earlier commit of this PR:\n{lines}\n</prior_findings>")
    parts.append(f"Review only this file: {label}\n\n<diff>\n{annotated_patch}\n</diff>")
    return "\n\n".join(parts)


def determines_outcome(overall_severity: str, threshold: str, complete: bool = True, on_incomplete: str = "fail") -> str:
    """
    The gate: 'block' for a finding at or above the threshold; otherwise 'fail' or
    'neutral' (per on_incomplete) if any file went unreviewed; otherwise 'pass'.
    """
    if SEVERITY_ORDER.index(overall_severity) >= SEVERITY_ORDER.index(threshold):
        return "block"
    if not complete:
        return "fail" if on_incomplete == "fail" else "neutral"
    return "pass"


def highest_severity(issues: list) -> str:
    if not issues:
        return "suggestion"
    return max((issue["severity"] for issue in issues), key=SEVERITY_ORDER.index)


def api_key_available(config: dict) -> bool:
    provider = config["provider"]
    return bool(
        os.environ.get("PAUL_API_KEY")
        or os.environ.get(_API_KEY_ENV[provider])
        or (provider == "google" and os.environ.get("GEMINI_API_KEY"))
    )


def resolve_model(config: dict) -> str:
    """The LiteLLM model string: provider prefix + model name."""
    model = config["model"]
    prefix, _, rest = model.partition("/")
    if rest and prefix == "google":
        return f"gemini/{rest}"
    if rest and prefix in _LITELLM_PREFIXES:
        return model
    return f"{_PROVIDER_PREFIX[config['provider']]}/{model}"


def supports_sampling(model: str) -> bool:
    return not _NO_SAMPLING_MODELS.search(model)


def set_deadline(deadline: float | None) -> None:
    """Stop starting LLM calls and retries after this time.monotonic() value."""
    global _deadline
    _deadline = deadline


def seconds_left() -> float | None:
    return None if _deadline is None else _deadline - time.monotonic()


# ── LLM call ─────────────────────────────────────────────────────────────────

def _call_for_json(system_prompt: str, content: str, config: dict, validate) -> dict:
    """Call the LLM and parse its JSON object, asking once more if the output is unusable."""
    for attempt in (1, 2):
        raw = _call_llm(system_prompt, content, config)
        try:
            return validate(_extract_json(raw))
        except InvalidOutput as e:
            _debug(f"Unusable output:\n{raw}")
            if attempt == 2:
                raise
            print(f"    Unusable output ({e}); asking again.")


def _call_llm(system_prompt: str, content: str, config: dict) -> str:
    _set_api_key_env(config)
    model = resolve_model(config)

    timeout = LLM_TIMEOUT_SECONDS
    left = seconds_left()
    if left is not None:
        if left < 15:
            raise OutOfTime("time budget reached")
        timeout = min(timeout, left)

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_content(system_prompt, model)},
            {"role": "user", "content": content},
        ],
        "max_tokens": _max_output_tokens(model, config["max_tokens"]),
        "response_format": {"type": "json_object"},
        "timeout": timeout,
    }
    if config.get("temperature") is not None and supports_sampling(model):
        kwargs["temperature"] = config["temperature"]

    response = _completion_with_backoff(kwargs)
    _record_usage(response)

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutput(f"response hit max_tokens ({kwargs['max_tokens']})")
    if choice.finish_reason == "content_filter" or not choice.message.content:
        raise Refused(f"no review content (finish_reason={choice.finish_reason})")
    return choice.message.content


def _system_content(system_prompt: str, model: str):
    """
    On Anthropic the system prompt goes out as a cached block. It is identical for
    every file in a run, so after the first call it is read from the prompt cache
    at a tenth of the input price. OpenAI and Gemini cache long prefixes on their own.
    """
    if model.startswith("anthropic/"):
        return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    return system_prompt


def _completion_with_backoff(kwargs: dict):
    """litellm.completion, retried on rate limits, overloads, server errors and timeouts."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return litellm.completion(**kwargs)
        except litellm.exceptions.ContextWindowExceededError as e:
            raise InputTooLarge(str(e)) from e
        except litellm.exceptions.ContentPolicyViolationError as e:
            raise Refused(str(e)) from e
        except litellm.exceptions.BadRequestError as e:
            if "temperature" in kwargs and "temperature" in str(e).lower():
                print("    The model rejected temperature; retrying without it.")
                kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
                continue
            raise ReviewError(f"request rejected: {e}") from e
        except _RETRYABLE as e:
            if attempt > len(_RETRY_DELAYS):
                raise LLMUnavailable(f"{type(e).__name__} after {attempt} attempts") from e
            delay = _retry_delay(e, attempt)
            left = seconds_left()
            if left is not None and delay + 15 > left:
                raise OutOfTime(f"{type(e).__name__}; no time left to retry") from e
            print(f"    {type(e).__name__} (attempt {attempt}); retrying in {delay:.0f}s...")
            time.sleep(delay)
        except litellm.exceptions.APIError as e:
            raise ReviewError(f"provider error: {e}") from e


def _retry_delay(error: Exception, attempt: int) -> float:
    retry_after = _retry_after_seconds(error)
    if retry_after is not None:
        return min(retry_after, 120.0)
    return _RETRY_DELAYS[attempt - 1] * random.uniform(0.8, 1.2)


def _retry_after_seconds(error: Exception) -> float | None:
    """The provider's retry-after hint, from LiteLLM's copy of the response headers or the response itself."""
    candidates = (
        getattr(error, "litellm_response_headers", None),
        getattr(getattr(error, "response", None), "headers", None),
    )
    for headers in candidates:
        if not headers:
            continue
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        try:
            if lowered.get("retry-after-ms"):
                return float(lowered["retry-after-ms"]) / 1000
            if lowered.get("retry-after"):
                return float(lowered["retry-after"])
        except (TypeError, ValueError):
            continue
    return None


def _max_output_tokens(model: str, requested: int) -> int:
    """The configured max_tokens, capped at what the model can produce."""
    try:
        limit = litellm.get_model_info(model).get("max_output_tokens")
    except Exception:  # LiteLLM raises a bare Exception for models missing from its map
        limit = None
    return min(requested, limit) if limit else requested


def _record_usage(response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or getattr(details, "cached_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    USAGE["calls"] += 1
    USAGE["input_tokens"] += prompt_tokens
    USAGE["cache_read_tokens"] += cache_read
    USAGE["cache_write_tokens"] += cache_write
    USAGE["output_tokens"] += output_tokens
    print(f"    tokens: {prompt_tokens:,} in ({cache_read:,} read from cache, "
          f"{cache_write:,} written to cache), {output_tokens:,} out")


def _set_api_key_env(config: dict) -> None:
    """Map the generic PAUL_API_KEY to the provider-specific env var LiteLLM expects."""
    api_key = os.environ.get("PAUL_API_KEY", "")
    if not api_key:
        return
    env_var = _API_KEY_ENV[config["provider"]]
    if not os.environ.get(env_var):
        os.environ[env_var] = api_key


def _debug(message: str) -> None:
    """Print only when the workflow runs with debug logging (RUNNER_DEBUG=1): raw model output can be long."""
    if os.environ.get("RUNNER_DEBUG") == "1":
        print(message)


# ── Prompts ──────────────────────────────────────────────────────────────────

def _build_prompt(path: Path, config: dict) -> str:
    return _render_prompt(
        str(path),
        config.get("repo_context", ""),
        config.get("custom_instructions", ""),
        config.get("language", ""),
    )


@functools.lru_cache(maxsize=8)
def _render_prompt(path: str, repo_context: str, custom_instructions: str, language: str) -> str:
    template = Path(path).read_text(encoding="utf-8")

    repo_ctx = repo_context.strip()
    ctx_block = (
        f"\n## Codebase Context\n\n"
        f"The following files describe this repo's conventions and rules. "
        f"Use them to avoid suggesting changes that conflict with established patterns.\n\n"
        f"{repo_ctx}\n"
    ) if repo_ctx else ""

    custom = custom_instructions.strip()
    custom_block = f"\n## Repo-Specific Instructions\n\n{custom}\n" if custom else ""
    if language.strip():
        custom_block += (
            f"\n## Language\n\n"
            f"Write every human-readable string (summaries, titles, descriptions, impacts, fixes and "
            f"test recommendations) in {language.strip()}. Keep JSON keys, severity values and code unchanged.\n"
        )

    blocks = {"REPO_CONTEXT": ctx_block, "CUSTOM_INSTRUCTIONS": custom_block}
    # One pass, so a placeholder that appears inside inserted text (a README that
    # documents the template, say) is left alone instead of being expanded again.
    return re.sub(r"\{(REPO_CONTEXT|CUSTOM_INSTRUCTIONS)\}", lambda m: blocks[m.group(1)], template)


# ── Parsing and normalization ────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """
    The JSON object in the response, tolerating code fences and prose around it.
    Prose that holds two objects is ambiguous (an echoed template next to the real
    answer, say), so it counts as unusable rather than trusting the first one.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        objects = []
        decoder = json.JSONDecoder()
        pos = 0
        while (start := text.find("{", pos)) != -1:
            try:
                obj, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                pos = start + 1
                continue
            objects.append(obj)
            pos = end
        if not objects:
            raise InvalidOutput("no JSON object in the response")
        if len(objects) > 1:
            raise InvalidOutput("more than one JSON object in the response")
        data = objects[0]
    if not isinstance(data, dict):
        raise InvalidOutput("the response is not a JSON object")
    return data


def normalize_severity(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    for word in re.findall(r"[a-z]+", text):  # "Severity: high" → "high"
        for prefix, severity in _SEVERITY_PREFIXES:
            if word.startswith(prefix):
                return severity
    print(f"    Unknown severity {value!r}; treating it as critical.")
    return "critical"


def normalize_file_review(data: dict, file_path: str) -> dict:
    if "issues" not in data:
        raise InvalidOutput("the response has no 'issues' field")
    raw_issues = data.get("issues") or []
    if isinstance(raw_issues, dict):
        raw_issues = [raw_issues]
    if not isinstance(raw_issues, list):
        raise InvalidOutput("'issues' is not a list")

    issues = []
    for item in raw_issues:
        # A finding written as a bare string, or an object with neither title nor
        # description, can't be shown or gated reliably: ask for the review again.
        if not isinstance(item, dict):
            raise InvalidOutput("an issue is not a JSON object")
        if not item.get("title") and not item.get("description"):
            raise InvalidOutput("an issue has neither a title nor a description")
        suggestion = item.get("suggestion")
        if isinstance(suggestion, str):
            suggestion = {"explanation": suggestion}
        elif not isinstance(suggestion, dict):
            suggestion = {}
        autofix = suggestion.get("autofix")
        line_start = _as_int(item.get("line_start"))
        issues.append({
            "severity": normalize_severity(item.get("severity")),
            "file": file_path,  # from the request, never from the model
            "line_start": line_start,
            "line_end": _as_int(item.get("line_end")) or line_start,
            "title": _as_text(item.get("title")) or "Untitled finding",
            "description": _as_text(item.get("description")),
            "impact": _as_text(item.get("impact")),
            "suggestion": {
                "explanation": _as_text(suggestion.get("explanation")),
                "autofix": autofix if isinstance(autofix, dict) else None,
            },
        })

    return {
        "issues": issues,
        "test_recommendations": _as_text_list(data.get("test_recommendations")),
        "resolved_prior_findings": _as_text_list(data.get("resolved_prior_findings")),
    }


def _normalize_walkthrough(data: dict) -> dict:
    if "summary" not in data and "changes" not in data:
        raise InvalidOutput("the response has neither 'summary' nor 'changes'")
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []
    return {
        "summary": _as_text(data.get("summary")),
        "changes": [
            {"file": _as_text(c.get("file")), "summary": _as_text(c.get("summary"))}
            for c in changes if isinstance(c, dict)
        ],
    }


def _as_int(value) -> int | None:
    """A positive line number, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON Infinity
        return None
    return number if number > 0 else None


def _as_text(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Drop lone surrogates, which can't be encoded as UTF-8 for the GitHub API.
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _as_text_list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [_as_text(item) for item in value if item not in (None, "")]


def _line_ref(finding: dict) -> str:
    start, end = finding.get("line_start"), finding.get("line_end")
    if start and end and start != end:
        return f"lines {start}-{end}"
    return f"line {start}" if start else "file"
