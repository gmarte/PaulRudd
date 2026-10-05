"""The Claude transport: request layout, model capabilities, error mapping and replies."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import llm as llm_core
import llm_anthropic
from schemas import ENVELOPE

PLAN = llm_core.PromptPlan("RULES", "REPO", "PR", "TASK")
SONNET_55 = {"structured_outputs": True, "efforts": {"low", "medium", "high", "xhigh", "max"}, "max_output": 128000}
SONNET_46 = {"structured_outputs": False, "efforts": {"low", "medium", "high", "max"}, "max_output": 64000}


def _config(**overrides):
    base = {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 16000, "temperature": None,
            "effort": "medium", "cache": {"ttl": "5m"}}
    return {**base, **overrides}


def _with_caps(monkeypatch, caps):
    monkeypatch.setattr(llm_anthropic, "capabilities", lambda model: caps)


# ── Request layout ───────────────────────────────────────────────────────────

def test_three_cache_breakpoints_then_the_uncached_task(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    request = llm_anthropic.build_request(PLAN, _config())
    assert request["system"] == [
        {"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "REPO", "cache_control": {"type": "ephemeral"}},
    ]
    assert request["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "PR", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "TASK"},
    ]}]


def test_one_hour_ttl_applies_to_the_rules_and_repo_blocks_only(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    request = llm_anthropic.build_request(PLAN, _config(cache={"ttl": "1h"}))
    assert [b["cache_control"] for b in request["system"]] == [{"type": "ephemeral", "ttl": "1h"}] * 2
    assert request["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}  # 1h must come first


def test_an_empty_repo_context_adds_no_block(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    request = llm_anthropic.build_request(llm_core.PromptPlan("RULES", "", "PR", "TASK"), _config())
    assert len(request["system"]) == 1


def test_claude_5_gets_structured_outputs_effort_and_refusal_fallbacks(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    request = llm_anthropic.build_request(PLAN, _config(temperature=0))
    assert request["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": ENVELOPE}}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert "extra_body" not in request  # sampling parameters are rejected on 5.x


def test_sonnet_4_6_gets_effort_and_temperature_but_no_schema_or_fallbacks(monkeypatch):
    _with_caps(monkeypatch, SONNET_46)
    request = llm_anthropic.build_request(PLAN, _config(model="claude-sonnet-4-6", temperature=0))
    assert request["output_config"] == {"effort": "medium"}
    assert request["extra_body"] == {"temperature": 0}  # SDK 1.x has no temperature argument
    assert "fallbacks" not in request and "betas" not in request


@pytest.mark.parametrize("effort, caps_efforts, sent", [
    ("medium", set(), False),               # the model has no effort control
    ("xhigh", {"low", "medium", "high"}, False),
    ("", SONNET_55["efforts"], False),       # "" leaves it to the model
])
def test_effort_is_sent_only_when_the_model_supports_that_level(monkeypatch, effort, caps_efforts, sent):
    _with_caps(monkeypatch, {"structured_outputs": False, "efforts": caps_efforts, "max_output": None})
    request = llm_anthropic.build_request(PLAN, _config(effort=effort))
    assert ("effort" in request.get("output_config", {})) is sent


def test_max_tokens_is_capped_at_the_models_limit(monkeypatch):
    _with_caps(monkeypatch, {**SONNET_46, "max_output": 8192})
    assert llm_anthropic.build_request(PLAN, _config(max_tokens=32768))["max_tokens"] == 8192


def test_prefixed_model_names_are_accepted(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    assert llm_anthropic.build_request(PLAN, _config(model="anthropic/claude-sonnet-5-5"))["model"] == "claude-sonnet-5-5"


# ── Capabilities ─────────────────────────────────────────────────────────────

class FakeClient:
    """The slice of anthropic.Anthropic that the transport uses."""

    def __init__(self, create=None, retrieve=None):
        self.requests = []
        self._create, self._retrieve = create, retrieve
        self.messages = SimpleNamespace(create=self._send)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._send))
        self.models = SimpleNamespace(retrieve=lambda model: self._retrieve(model))

    def with_options(self, **kwargs):
        return self

    def _send(self, **request):
        self.requests.append(request)
        return self._create(request)


def _use(monkeypatch, client):
    monkeypatch.setattr(llm_anthropic, "_client", lambda base_url: client)
    llm_anthropic.capabilities.cache_clear()


def _status_error(cls, status, message="error", headers=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request, headers=headers or {}), body=None)


def test_capabilities_come_from_the_models_api(monkeypatch):
    info = SimpleNamespace(max_tokens=128000, capabilities={
        "structured_outputs": {"supported": True},
        "effort": {"supported": True, "low": {"supported": True}, "medium": {"supported": True},
                   "high": {"supported": True}, "xhigh": {"supported": False}, "max": {"supported": True}},
    })
    _use(monkeypatch, FakeClient(retrieve=lambda model: info))
    assert llm_anthropic.capabilities("claude-sonnet-5-5") == {
        "structured_outputs": True, "efforts": {"low", "medium", "high", "max"}, "max_output": 128000}


def test_capabilities_read_the_sdks_typed_object(monkeypatch):
    # Caught by the contract test: the SDK returns ModelCapabilities, not a dict.
    from anthropic._models import construct_type
    from anthropic.types.model_info import ModelInfo
    info = construct_type(type_=ModelInfo, value={
        "id": "claude-sonnet-5-5", "type": "model", "max_tokens": 128000,
        "capabilities": {"structured_outputs": {"supported": True},
                         "effort": {"supported": True, "medium": {"supported": True}, "high": {"supported": True}}}})
    _use(monkeypatch, FakeClient(retrieve=lambda model: info))
    assert llm_anthropic.capabilities("claude-sonnet-5-5") == {
        "structured_outputs": True, "efforts": {"medium", "high"}, "max_output": 128000}


def test_documented_defaults_when_the_models_api_is_unreachable(monkeypatch):
    def unreachable(model):
        raise anthropic.APIConnectionError(request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"))

    _use(monkeypatch, FakeClient(retrieve=unreachable))
    assert llm_anthropic.capabilities("claude-sonnet-4-6") == {
        "structured_outputs": False, "efforts": {"low", "medium", "high"}, "max_output": None}
    assert llm_anthropic.capabilities("claude-sonnet-5-5")["structured_outputs"] is True


def test_a_bad_key_is_not_mistaken_for_missing_capabilities(monkeypatch):
    def rejected(model):
        raise _status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")

    _use(monkeypatch, FakeClient(retrieve=rejected))
    with pytest.raises(anthropic.AuthenticationError):
        llm_anthropic.capabilities("claude-sonnet-5-5")


# ── Errors and replies ───────────────────────────────────────────────────────

def _message(text='{"issues": []}', stop="end_turn", model="claude-sonnet-5-5", **usage):
    fields = {"input_tokens": 300, "output_tokens": 20, "cache_read_input_tokens": 40000,
              "cache_creation_input_tokens": 0, "cache_creation": None, **usage}
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=SimpleNamespace(**fields))


def _send_with(monkeypatch, create, config=None):
    client = FakeClient(create=create)
    _use(monkeypatch, client)
    monkeypatch.setattr(llm_anthropic, "capabilities", lambda model: SONNET_55)
    return client, llm_anthropic.send(PLAN, config or _config(), timeout=30)


def test_a_reply_maps_text_stop_reason_and_usage(monkeypatch):
    _, reply = _send_with(monkeypatch, lambda request: _message())
    assert reply.text == '{"issues": []}' and reply.stop_reason == "end"
    assert (reply.usage.fresh_input, reply.usage.cache_read, reply.usage.output) == (300, 40000, 20)
    assert reply.usage.cost_usd == pytest.approx((300 * 2 + 40000 * 0.2 + 20 * 10) / 1_000_000)


@pytest.mark.parametrize("stop, expected", [("max_tokens", "max_tokens"), ("refusal", "refusal"), ("end_turn", "end")])
def test_stop_reasons(monkeypatch, stop, expected):
    _, reply = _send_with(monkeypatch, lambda request: _message(stop=stop))
    assert reply.stop_reason == expected


@pytest.mark.parametrize("error, raised", [
    (_status_error(anthropic.OverloadedError, 529, headers={"retry-after": "7"}), llm_core.Retry),
    (_status_error(anthropic.RateLimitError, 429), llm_core.Retry),
    (_status_error(anthropic.APIStatusError, 502), llm_core.Retry),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), llm_core.Retry),
    (_status_error(anthropic.RequestTooLargeError, 413), llm_core.InputTooLarge),
    (_status_error(anthropic.BadRequestError, 400, "prompt is too long: 250000 tokens > 200000"), llm_core.InputTooLarge),
    (_status_error(anthropic.BadRequestError, 400, "messages: something else"), llm_core.ReviewError),
    (_status_error(anthropic.AuthenticationError, 401), anthropic.AuthenticationError),  # fails the run
])
def test_errors_map_to_retry_fail_the_file_or_fail_the_run(monkeypatch, error, raised):
    def create(request):
        raise error

    with pytest.raises(raised):
        _send_with(monkeypatch, create)


def test_retry_after_comes_from_the_response_headers(monkeypatch):
    def create(request):
        raise _status_error(anthropic.OverloadedError, 529, headers={"retry-after": "7"})

    with pytest.raises(llm_core.Retry) as caught:
        _send_with(monkeypatch, create)
    assert caught.value.retry_after == 7.0


def test_a_rejected_temperature_is_retried_without_it(monkeypatch):
    def create(request):
        if "temperature" in request.get("extra_body", {}):
            raise _status_error(anthropic.BadRequestError, 400, "temperature is not supported for this model")
        return _message()

    monkeypatch.setattr(llm_anthropic, "_NO_SAMPLING_MODELS", llm_anthropic.re.compile("nothing-matches"))
    client, reply = _send_with(monkeypatch, create, _config(temperature=0))
    assert ["extra_body" in r for r in client.requests] == [True, False] and reply.stop_reason == "end"


def test_fallback_models_go_through_the_beta_endpoint(monkeypatch):
    client = FakeClient(create=lambda request: _message())
    _use(monkeypatch, client)
    monkeypatch.setattr(llm_anthropic, "capabilities", lambda model: SONNET_55)
    beta_calls = []
    client.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **r: beta_calls.append(r) or _message()))
    llm_anthropic.send(PLAN, _config(), timeout=30)
    assert len(beta_calls) == 1 and beta_calls[0]["fallbacks"] == "default"
