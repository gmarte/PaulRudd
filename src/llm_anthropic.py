"""
The Claude transport, on the official Anthropic SDK.

Each request is laid out for prompt caching with three breakpoints:

  system[0]  static rules   cache_control (cache.ttl: 5m or 1h)
  system[1]  repo context   cache_control (cache.ttl)
  user[0]    PR context     cache_control (5m: it changes on every push)
  user[1]    task           not cached

Every call in a run uses the same model, effort and output schema, so every
call after the first reads the whole prefix from the cache.

What a model supports (structured outputs, effort levels, its output limit)
comes from the Models API, with documented defaults if that lookup fails.
Every call streams: a long answer then can't trip the per-request timeout,
and the run's time budget is checked while it arrives.
"""

import functools
import os
import re

import anthropic
import httpx2

import llm
from schemas import ENVELOPE

# Models whose safety classifiers can decline a request and that accept the
# server-side refusal fallback (fallbacks: "default") on the Claude API.
_FALLBACK_MODELS = re.compile(r"^claude-(?:opus-5|fable-5|sonnet-5-5)")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that reject sampling parameters such as temperature.
_NO_SAMPLING_MODELS = re.compile(r"claude-(?:opus-4-[78]|opus-5|sonnet-5|fable|mythos)")
# Models that think before answering by default; the thinking counts toward max_tokens.
_THINKING_MODELS = re.compile(r"claude-(?:opus-5|sonnet-5|fable|mythos)")
_MIN_TOKENS_WITH_THINKING = 16_000
# Used only when the Models API can't be asked.
_STRUCTURED_OUTPUT_MODELS = re.compile(
    r"claude-(?:fable-5|mythos-5|opus-5|opus-4-8|opus-4-5|opus-4-1|sonnet-5|haiku-4-5)")
_EFFORT_MODELS = re.compile(r"claude-(?:fable|mythos|opus-5|opus-4-[5-8]|sonnet-5|sonnet-4-6)")
_TOO_LONG = re.compile(r"prompt is too long|too many (?:input )?tokens|context (?:window|length|limit)", re.I)

# Error types worth retrying. A stream that fails part-way reports its error with
# the stream's status (200), so the type is what tells an overload apart.
_RETRY_TYPES = {"rate_limit_error", "overloaded_error", "api_error", "timeout_error"}
_RETRY_STATUSES = {408, 409, 429}
# A bad key, an unknown model or a missing permission: every call would fail the same way.
_FATAL = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)

# Request parameters the API rejected earlier in this run, left out from then on.
_dropped = set()
_noted = set()


def reset() -> None:
    """Forget what earlier calls learned (for tests; a real run is one process)."""
    _dropped.clear()
    _noted.clear()
    capabilities.cache_clear()
    _client.cache_clear()


def model_id(config: dict) -> str:
    model = config["model"]
    return model.split("/", 1)[1] if model.startswith("anthropic/") else model


def send(plan: llm.PromptPlan, config: dict, timeout: float) -> llm.Reply:
    request = build_request(plan, config)
    while True:
        try:
            message = _create(request, timeout)
            break
        except anthropic.APIStatusError as e:
            # _create re-raises a 400 only when it names a parameter Paul can do without.
            param = _rejected_param(e, request)
            if not param:
                raise
            _dropped.add(param)
            llm.log(f"    The API rejected `{param}` for {request['model']}; leaving it out for the rest of the run.")
            request = build_request(plan, config)
    return _reply(message, request["model"])


def build_request(plan: llm.PromptPlan, config: dict) -> dict:
    model = model_id(config)
    caps = capabilities(model)
    cache = {"type": "ephemeral", "ttl": "1h"} if config["cache"]["ttl"] == "1h" else {"type": "ephemeral"}

    system = [{"type": "text", "text": plan.static_rules, "cache_control": cache}]
    if plan.repo_context:
        system.append({"type": "text", "text": plan.repo_context, "cache_control": cache})
    user = [{"type": "text", "text": plan.pr_context, "cache_control": {"type": "ephemeral"}}]
    if plan.task:
        user.append({"type": "text", "text": plan.task})

    max_tokens = config["max_tokens"]
    if _THINKING_MODELS.search(model) and max_tokens < _MIN_TOKENS_WITH_THINKING:
        _note_once(f"  max_tokens {max_tokens} leaves too little room for {model}, which thinks before "
                   f"answering; using {_MIN_TOKENS_WITH_THINKING}.")
        max_tokens = _MIN_TOKENS_WITH_THINKING
    if caps["max_output"]:
        max_tokens = min(max_tokens, caps["max_output"])
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    output_config = {}
    if config["effort"] and config["effort"] in caps["efforts"]:
        output_config["effort"] = config["effort"]
    if caps["structured_outputs"]:
        output_config["format"] = {"type": "json_schema", "schema": ENVELOPE}
    if output_config:
        request["output_config"] = output_config

    if (config.get("temperature") is not None and not _NO_SAMPLING_MODELS.search(model)
            and "temperature" not in _dropped):
        # SDK 1.x dropped sampling parameters from its signatures (newer models reject
        # them); models that still accept them get the value in the raw request body.
        request["extra_body"] = {"temperature": config["temperature"]}
    if _FALLBACK_MODELS.match(model) and "fallbacks" not in _dropped:
        # A declined request is re-run server-side on the model Anthropic recommends
        # for that refusal category, instead of coming back as a refusal.
        request["betas"] = [_FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


@functools.lru_cache(maxsize=8)
def capabilities(model: str) -> dict:
    """{'structured_outputs': bool, 'efforts': set of effort levels, 'max_output': int | None}."""
    try:
        info = _client(_base_url()).models.retrieve(model)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise llm.Fatal(e) from e
    except anthropic.APIError as e:
        # Includes a 404, which a gateway without the Models API returns too; an unknown
        # model still fails the run on its first call.
        llm.log(f"  Couldn't look up {model} in the Models API ({type(e).__name__}); using documented defaults.")
        return {
            "structured_outputs": bool(_STRUCTURED_OUTPUT_MODELS.search(model)),
            "efforts": {"low", "medium", "high"} if _EFFORT_MODELS.search(model) else set(),
            "max_output": None,
        }
    caps = info.capabilities or {}
    if not isinstance(caps, dict):  # the SDK returns a typed ModelCapabilities object
        caps = caps.to_dict()
    effort = caps.get("effort") or {}
    return {
        "structured_outputs": bool((caps.get("structured_outputs") or {}).get("supported")),
        "efforts": {
            level for level in ("low", "medium", "high", "xhigh", "max")
            if effort.get("supported") and (effort.get(level) or {}).get("supported")
        },
        "max_output": info.max_tokens,
    }


def _create(request: dict, timeout: float):
    client = _client(_base_url()).with_options(timeout=timeout)
    endpoint = client.beta.messages if "betas" in request else client.messages
    try:
        with endpoint.stream(**request) as stream:
            for _ in stream:
                left = llm.seconds_left()
                if left is not None and left < 0:
                    raise llm.OutOfTime("time_budget_minutes ran out during an LLM call")
            return stream.get_final_message()
    except _FATAL as e:
        raise llm.Fatal(e) from e
    except anthropic.APIStatusError as e:
        if e.type == "billing_error" or e.status_code == 402:
            raise llm.Fatal(e) from e
        if e.type in _RETRY_TYPES or e.status_code in _RETRY_STATUSES or e.status_code >= 500:
            raise llm.Retry(e, _retry_after(e)) from e
        if isinstance(e, anthropic.RequestTooLargeError) or _TOO_LONG.search(str(e)):
            raise llm.InputTooLarge(str(e)) from e
        if _rejected_param(e, request):
            raise  # send() retries without it
        raise llm.ReviewError(f"request rejected: {e}") from e
    except (anthropic.APIConnectionError, httpx2.TransportError) as e:
        # A dropped or stalled stream surfaces as the HTTP library's own error.
        raise llm.Retry(e) from e


def _rejected_param(error, request: dict) -> str | None:
    """The optional parameter a 400 rejected (temperature, or the fallback beta), if the request has it."""
    if not isinstance(error, anthropic.BadRequestError):
        return None
    message = str(error).lower()
    if "temperature" in request.get("extra_body", {}) and "temperature" in message:
        return "temperature"
    if "betas" in request and ("fallback" in message or "anthropic-beta" in message):
        return "fallbacks"
    return None


def _reply(message, requested_model: str) -> llm.Reply:
    # A refusal fallback can split the answer: [partial text, fallback marker, the rest].
    text = "".join(block.text for block in message.content if block.type == "text") or None
    stop = {
        "max_tokens": "max_tokens",
        "model_context_window_exceeded": "context_window",
        "refusal": "refusal",
    }.get(message.stop_reason, "end")
    served_model = getattr(message, "model", None) or requested_model
    return llm.Reply(text, stop, _usage(message.usage, served_model, requested_model))


def _usage(u, served_model: str, requested_model: str) -> llm.Usage:
    """
    Token counts and cost. With refusal fallbacks, usage.iterations lists every
    attempt with the model that ran it (each billed at that model's prices); the
    top-level usage then covers only the attempt that answered.
    """
    entries = list(getattr(u, "iterations", None) or []) or [u]
    total = llm.Usage(cost_usd=0.0)
    for entry in entries:
        part = llm.Usage(
            fresh_input=getattr(entry, "input_tokens", 0) or 0,
            cache_read=getattr(entry, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(entry, "cache_creation_input_tokens", 0) or 0,
            output=getattr(entry, "output_tokens", 0) or 0,
        )
        write_1h = getattr(getattr(entry, "cache_creation", None), "ephemeral_1h_input_tokens", 0) or 0
        model = getattr(entry, "model", None) or served_model
        cost = llm.anthropic_cost(model, part, write_1h)
        if cost is None:  # a model missing from the price table: price it as the configured one
            cost = llm.anthropic_cost(requested_model, part, write_1h)
        total.fresh_input += part.fresh_input
        total.cache_read += part.cache_read
        total.cache_write += part.cache_write
        total.output += part.output
        total.cost_usd = None if cost is None or total.cost_usd is None else total.cost_usd + cost
    return total


def _note_once(message: str) -> None:
    if message not in _noted:
        _noted.add(message)
        llm.log(message)


def _retry_after(error) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value:
            try:
                return float(value) * scale
            except ValueError:
                continue
    return None


def _base_url() -> str | None:
    return os.environ.get("ANTHROPIC_BASE_URL") or None


@functools.lru_cache(maxsize=4)
def _client(base_url: str | None) -> anthropic.Anthropic:
    # Paul retries on its own (llm.complete), within the run's time budget.
    return anthropic.Anthropic(base_url=base_url, max_retries=0)
