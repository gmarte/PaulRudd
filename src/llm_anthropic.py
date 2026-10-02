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
"""

import functools
import os
import re

import anthropic

import llm
from schemas import ENVELOPE

# Models whose safety classifiers can decline a request and that accept the
# server-side refusal fallback (fallbacks: "default") on the Claude API.
_FALLBACK_MODELS = re.compile(r"^claude-(?:opus-5|fable-5|sonnet-5-5)")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that reject sampling parameters such as temperature.
_NO_SAMPLING_MODELS = re.compile(r"claude-(?:opus-4-[78]|opus-5|sonnet-5|fable|mythos)")
# Used only when the Models API can't be asked.
_STRUCTURED_OUTPUT_MODELS = re.compile(
    r"claude-(?:fable-5|mythos-5|opus-5|opus-4-8|opus-4-5|opus-4-1|sonnet-5|haiku-4-5)")
_EFFORT_MODELS = re.compile(r"claude-(?:fable|mythos|opus-5|opus-4-[5-8]|sonnet-5|sonnet-4-6)")
# The SDK refuses non-streaming requests that could run past 10 minutes.
_STREAM_ABOVE_TOKENS = 20_000
_TOO_LONG = re.compile(r"prompt is too long|too many (?:input )?tokens|context (?:window|length)", re.I)

_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.InternalServerError,
    anthropic.DeadlineExceededError,
    anthropic.APIConnectionError,  # includes APITimeoutError
)
# A bad key, an unknown model or a missing permission: every call would fail the same way.
_FATAL = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)


def model_id(config: dict) -> str:
    model = config["model"]
    return model.split("/", 1)[1] if model.startswith("anthropic/") else model


def send(plan: llm.PromptPlan, config: dict, timeout: float) -> llm.Reply:
    request = build_request(plan, config)
    try:
        message = _create(request, timeout)
    except anthropic.BadRequestError as e:
        if "temperature" in request and "temperature" in str(e).lower():
            llm.log("    The model rejected temperature; retrying without it.")
            request.pop("temperature")
            message = _create(request, timeout)
        else:
            raise
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

    if config.get("temperature") is not None and not _NO_SAMPLING_MODELS.search(model):
        request["temperature"] = config["temperature"]
    if _FALLBACK_MODELS.match(model):
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
    except _FATAL:
        raise
    except anthropic.APIError as e:
        llm.log(f"  Couldn't look up {model} in the Models API ({type(e).__name__}); using documented defaults.")
        return {
            "structured_outputs": bool(_STRUCTURED_OUTPUT_MODELS.search(model)),
            "efforts": {"low", "medium", "high"} if _EFFORT_MODELS.search(model) else set(),
            "max_output": None,
        }
    caps = info.capabilities or {}
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
        if request["max_tokens"] > _STREAM_ABOVE_TOKENS:
            with endpoint.stream(**request) as stream:
                return stream.get_final_message()
        return endpoint.create(**request)
    except _FATAL:
        raise
    except anthropic.RequestTooLargeError as e:
        raise llm.InputTooLarge(str(e)) from e
    except anthropic.BadRequestError as e:
        if _TOO_LONG.search(str(e)):
            raise llm.InputTooLarge(str(e)) from e
        if "temperature" in request and "temperature" in str(e).lower():
            raise  # send() retries once without temperature
        raise llm.ReviewError(f"request rejected: {e}") from e
    except _RETRYABLE as e:
        raise llm.Retry(e, _retry_after(e)) from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise llm.Retry(e, _retry_after(e)) from e
        raise llm.ReviewError(f"request rejected: {e}") from e


def _reply(message, requested_model: str) -> llm.Reply:
    text = next((block.text for block in message.content if block.type == "text"), None)
    stop = {"max_tokens": "max_tokens", "refusal": "refusal"}.get(message.stop_reason, "end")
    u = message.usage
    usage = llm.Usage(
        fresh_input=u.input_tokens or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        output=u.output_tokens or 0,
    )
    write_1h = getattr(getattr(u, "cache_creation", None), "ephemeral_1h_input_tokens", 0) or 0
    # A request served by a refusal fallback is billed at the fallback model's prices.
    usage.cost_usd = llm.anthropic_cost(getattr(message, "model", None) or requested_model, usage, write_1h)
    return llm.Reply(text, stop, usage)


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
