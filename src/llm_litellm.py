"""
The OpenAI and Gemini transport, through LiteLLM (installed only for those providers).

The same prompt plan goes out stable-first, without cache markers: OpenAI caches
prompt prefixes of 1,024+ tokens on its own, and Gemini 2.5+ caches implicitly.
(Sending cache_control to Gemini through LiteLLM would create explicit caches,
which are billed for storage.)
"""

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no model-map download at import

import litellm  # noqa: E402

import llm  # noqa: E402
from schemas import ENVELOPE  # noqa: E402

_RETRYABLE = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.BadGatewayError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
)
_FATAL = (litellm.exceptions.AuthenticationError, litellm.exceptions.PermissionDeniedError,
          litellm.exceptions.NotFoundError)
_LITELLM_PREFIXES = {"openai", "gemini", "vertex_ai", "azure", "bedrock", "cohere"}
_PROVIDER_PREFIX = {"openai": "openai", "google": "gemini"}
# OpenAI's reasoning_effort stops at high.
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def resolve_model(config: dict) -> str:
    """The LiteLLM model string: provider prefix + model name."""
    model = config["model"]
    prefix, _, rest = model.partition("/")
    if rest and prefix == "google":
        return f"gemini/{rest}"
    if rest and prefix in _LITELLM_PREFIXES:
        return model
    return f"{_PROVIDER_PREFIX[config['provider']]}/{model}"


def send(plan: llm.PromptPlan, config: dict, timeout: float) -> llm.Reply:
    model = resolve_model(config)
    system = plan.static_rules + (f"\n\n{plan.repo_context}" if plan.repo_context else "")
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{plan.pr_context}\n\n{plan.task}".rstrip()},
        ],
        "max_tokens": _max_output_tokens(model, config["max_tokens"]),
        "response_format": _response_format(model),
        "timeout": timeout,
        "drop_params": True,  # per call, so parameters a model doesn't take are dropped, not rejected
    }
    if config["effort"]:
        kwargs["reasoning_effort"] = _EFFORT[config["effort"]]
    if config.get("temperature") is not None:
        kwargs["temperature"] = config["temperature"]
    if model.startswith("openai/"):
        # Routes requests with the same prefix to the same cache.
        kwargs["extra_body"] = {"prompt_cache_key": f"paul:{os.environ.get('REPO', '')}"}

    try:
        response = litellm.completion(**kwargs)
    except _FATAL:
        raise
    except litellm.exceptions.ContextWindowExceededError as e:
        raise llm.InputTooLarge(str(e)) from e
    except litellm.exceptions.ContentPolicyViolationError as e:
        raise llm.Refused(str(e)) from e
    except litellm.exceptions.BadRequestError as e:
        raise llm.ReviewError(f"request rejected: {e}") from e
    except _RETRYABLE as e:
        raise llm.Retry(e, _retry_after(e)) from e
    except litellm.exceptions.APIError as e:
        raise llm.ReviewError(f"provider error: {e}") from e

    choice = response.choices[0]
    stop = {"length": "max_tokens", "content_filter": "refusal"}.get(choice.finish_reason, "end")
    return llm.Reply(choice.message.content, stop, _usage(response))


def _response_format(model: str) -> dict:
    try:
        strict = litellm.supports_response_schema(model=model)
    except Exception:  # LiteLLM raises a bare Exception for models missing from its map
        strict = False
    if strict:
        return {"type": "json_schema", "json_schema": {"name": "paul_envelope", "strict": True, "schema": ENVELOPE}}
    return {"type": "json_object"}


def _max_output_tokens(model: str, requested: int) -> int:
    try:
        limit = litellm.get_model_info(model).get("max_output_tokens")
    except Exception:  # LiteLLM raises a bare Exception for models missing from its map
        limit = None
    return min(requested, limit) if limit else requested


def _usage(response) -> llm.Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return llm.Usage()
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0  # includes cached tokens
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = getattr(details, "cached_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:  # unknown model prices
        cost = None
    return llm.Usage(
        fresh_input=max(prompt_tokens - cache_read - cache_write, 0),
        cache_read=cache_read,
        cache_write=cache_write,
        output=getattr(usage, "completion_tokens", 0) or 0,
        cost_usd=cost,
    )


def _retry_after(error) -> float | None:
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
