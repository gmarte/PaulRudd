"""The Claude transport: request layout, model capabilities, streaming, error mapping and replies."""

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
    assert llm_anthropic.build_request(PLAN, _config(model="claude-sonnet-4-6", max_tokens=32768))["max_tokens"] == 8192


def test_models_that_think_get_room_for_it(monkeypatch, capsys):
    # The intranet's max_tokens: 4096 would cut Sonnet 5.5 off mid-thought.
    _with_caps(monkeypatch, SONNET_55)
    assert llm_anthropic.build_request(PLAN, _config(max_tokens=4096))["max_tokens"] == 16000
    llm_anthropic.build_request(PLAN, _config(max_tokens=4096))
    assert capsys.readouterr().out.count("leaves too little room") == 1  # said once per run
    _with_caps(monkeypatch, SONNET_46)
    assert llm_anthropic.build_request(PLAN, _config(model="claude-sonnet-4-6", max_tokens=4096))["max_tokens"] == 4096


def test_prefixed_model_names_are_accepted(monkeypatch):
    _with_caps(monkeypatch, SONNET_55)
    assert llm_anthropic.build_request(PLAN, _config(model="anthropic/claude-sonnet-5-5"))["model"] == "claude-sonnet-5-5"


# ── A stand-in for the SDK client ────────────────────────────────────────────

class _Stream:
    """messages.stream(...): an exception here is raised part-way through the stream."""

    def __init__(self, outcome):
        self._outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="message_start")
        if isinstance(self._outcome, BaseException):
            raise self._outcome

    def get_final_message(self):
        return self._outcome


class FakeClient:
    """
    The slice of anthropic.Anthropic that the transport uses. `create(request)`
    returns a message, returns an exception to raise mid-stream, or raises one
    (as the SDK does for an error status when the stream opens).
    """

    def __init__(self, create=None, retrieve=None):
        self.requests = []
        self.endpoints = []
        self._create, self._retrieve = create, retrieve
        self.messages = SimpleNamespace(stream=lambda **r: self._send("messages", r))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **r: self._send("beta", r)))
        self.models = SimpleNamespace(retrieve=lambda model: self._retrieve(model))

    def with_options(self, **kwargs):
        return self

    def _send(self, endpoint, request):
        self.requests.append(request)
        self.endpoints.append(endpoint)
        return _Stream(self._create(request))


def _use(monkeypatch, client):
    monkeypatch.setattr(llm_anthropic, "_client", lambda base_url: client)


def _status_error(cls, status, message="error", headers=None, error_type=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": error_type, "message": message}} if error_type else None
    return cls(message, response=httpx2.Response(status, request=request, headers=headers or {}), body=body)


# ── Capabilities ─────────────────────────────────────────────────────────────

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


@pytest.mark.parametrize("error", [
    anthropic.APIConnectionError(request=httpx2.Request("GET", "https://api.anthropic.com/v1/models")),
    _status_error(anthropic.NotFoundError, 404),  # a gateway without the Models API
])
def test_documented_defaults_when_the_models_api_cant_answer(monkeypatch, error):
    def unavailable(model):
        raise error

    _use(monkeypatch, FakeClient(retrieve=unavailable))
    assert llm_anthropic.capabilities("claude-sonnet-4-6") == {
        "structured_outputs": False, "efforts": {"low", "medium", "high"}, "max_output": None}
    assert llm_anthropic.capabilities("claude-sonnet-5-5")["structured_outputs"] is True


def test_a_bad_key_is_not_mistaken_for_missing_capabilities(monkeypatch):
    def rejected(model):
        raise _status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")

    _use(monkeypatch, FakeClient(retrieve=rejected))
    with pytest.raises(llm_core.Fatal, match="invalid x-api-key"):
        llm_anthropic.capabilities("claude-sonnet-5-5")


# ── Errors and replies ───────────────────────────────────────────────────────

def _message(text='{"issues": []}', stop="end_turn", model="claude-sonnet-5-5", content=None, **usage):
    fields = {"input_tokens": 300, "output_tokens": 20, "cache_read_input_tokens": 40000,
              "cache_creation_input_tokens": 0, "cache_creation": None, **usage}
    if content is None:
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=SimpleNamespace(**fields))


def _send_with(monkeypatch, create, config=None, caps=SONNET_55):
    client = FakeClient(create=create)
    _use(monkeypatch, client)
    monkeypatch.setattr(llm_anthropic, "capabilities", lambda model: caps)
    return client, llm_anthropic.send(PLAN, config or _config(), timeout=30)


def test_a_reply_maps_text_stop_reason_and_usage(monkeypatch):
    _, reply = _send_with(monkeypatch, lambda request: _message())
    assert reply.text == '{"issues": []}' and reply.stop_reason == "end"
    assert (reply.usage.fresh_input, reply.usage.cache_read, reply.usage.output) == (300, 40000, 20)
    assert reply.usage.cost_usd == pytest.approx((300 * 2 + 40000 * 0.2 + 20 * 10) / 1_000_000)


@pytest.mark.parametrize("stop, expected", [
    ("max_tokens", "max_tokens"), ("model_context_window_exceeded", "context_window"),
    ("refusal", "refusal"), ("end_turn", "end"),
])
def test_stop_reasons(monkeypatch, stop, expected):
    _, reply = _send_with(monkeypatch, lambda request: _message(stop=stop))
    assert reply.stop_reason == expected


def test_an_answer_split_by_a_refusal_fallback_is_joined(monkeypatch):
    # A streamed decline keeps the partial answer, marks the switch, and continues after it.
    content = [SimpleNamespace(type="text", text='{"issues": '),
               SimpleNamespace(type="fallback"),
               SimpleNamespace(type="text", text="[]}")]
    _, reply = _send_with(monkeypatch, lambda request: _message(content=content))
    assert reply.text == '{"issues": []}'


def test_every_fallback_attempt_is_billed_at_its_own_models_prices(monkeypatch):
    iterations = [
        SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=1000, output_tokens=100,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None),
        SimpleNamespace(type="fallback_message", model="claude-opus-5-5", input_tokens=1000, output_tokens=500,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None),
    ]
    _, reply = _send_with(monkeypatch, lambda request: _message(model="claude-opus-5-5", iterations=iterations))
    sonnet = (1000 * 2 + 100 * 10) / 1_000_000
    opus = (1000 * 4 + 500 * 20) / 1_000_000
    assert reply.usage.cost_usd == pytest.approx(sonnet + opus)
    assert (reply.usage.fresh_input, reply.usage.output) == (2000, 600)


def test_a_model_missing_from_the_price_table_is_priced_as_the_configured_one(monkeypatch):
    _, reply = _send_with(monkeypatch, lambda request: _message(model="claude-sonnet-9"))
    assert reply.usage.cost_usd == pytest.approx((300 * 2 + 40000 * 0.2 + 20 * 10) / 1_000_000)


def _overloaded_mid_stream():
    # A stream that fails part-way reports the error with the stream's status, 200.
    return _status_error(anthropic.APIStatusError, 200, "Overloaded", error_type="overloaded_error")


@pytest.mark.parametrize("error, raised", [
    (_status_error(anthropic.OverloadedError, 529, headers={"retry-after": "7"}), llm_core.Retry),
    (_status_error(anthropic.RateLimitError, 429), llm_core.Retry),
    (_status_error(anthropic.APIStatusError, 502), llm_core.Retry),
    (_status_error(anthropic.APIStatusError, 408), llm_core.Retry),
    (_status_error(anthropic.ConflictError, 409), llm_core.Retry),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), llm_core.Retry),
    (_status_error(anthropic.RequestTooLargeError, 413), llm_core.InputTooLarge),
    (_status_error(anthropic.BadRequestError, 400, "prompt is too long: 250000 tokens > 200000"), llm_core.InputTooLarge),
    (_status_error(anthropic.BadRequestError, 400, "input length and `max_tokens` exceed context limit"),
     llm_core.InputTooLarge),
    (_status_error(anthropic.BadRequestError, 400, "messages: something else"), llm_core.ReviewError),
    (_status_error(anthropic.AuthenticationError, 401), llm_core.Fatal),   # ends the run
    (_status_error(anthropic.APIStatusError, 402, "credit balance is too low", error_type="billing_error"),
     llm_core.Fatal),
])
def test_errors_map_to_retry_fail_the_file_or_end_the_run(monkeypatch, error, raised):
    def create(request):
        raise error

    with pytest.raises(raised):
        _send_with(monkeypatch, create)


@pytest.mark.parametrize("error", [
    _overloaded_mid_stream(),
    httpx2.RemoteProtocolError("peer closed connection without sending complete message body"),
    httpx2.ReadTimeout("timed out"),
])
def test_a_stream_that_fails_part_way_is_retried(monkeypatch, error):
    # Review findings: these used to crash the run (transport errors) or fail the file (overloads).
    with pytest.raises(llm_core.Retry):
        _send_with(monkeypatch, lambda request: error)


def test_retry_after_comes_from_the_response_headers(monkeypatch):
    def create(request):
        raise _status_error(anthropic.OverloadedError, 529, headers={"retry-after": "7"})

    with pytest.raises(llm_core.Retry) as caught:
        _send_with(monkeypatch, create)
    assert caught.value.retry_after == 7.0


def test_the_time_budget_is_checked_while_the_answer_streams(monkeypatch):
    llm_core.set_deadline(llm_core.time.monotonic() - 1)  # ran out after the call started
    with pytest.raises(llm_core.OutOfTime):
        _send_with(monkeypatch, lambda request: _message())


def test_a_rejected_temperature_is_dropped_for_the_rest_of_the_run(monkeypatch):
    def create(request):
        if "temperature" in request.get("extra_body", {}):
            raise _status_error(anthropic.BadRequestError, 400, "temperature is not supported for this model")
        return _message()

    client, reply = _send_with(monkeypatch, create, _config(model="claude-sonnet-4-6", temperature=0), caps=SONNET_46)
    assert ["extra_body" in r for r in client.requests] == [True, False] and reply.stop_reason == "end"
    llm_anthropic.send(PLAN, _config(model="claude-sonnet-4-6", temperature=0), timeout=30)
    assert "extra_body" not in client.requests[-1]  # not sent again


def test_a_rejected_fallback_beta_is_dropped_for_the_rest_of_the_run(monkeypatch):
    def create(request):
        if "betas" in request:
            raise _status_error(anthropic.BadRequestError, 400,
                                "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header")
        return _message()

    client, reply = _send_with(monkeypatch, create)
    assert client.endpoints == ["beta", "messages"] and "fallbacks" not in client.requests[-1]
    llm_anthropic.send(PLAN, _config(), timeout=30)
    assert client.endpoints[-1] == "messages"


def test_fallback_models_go_through_the_beta_endpoint(monkeypatch):
    client, _ = _send_with(monkeypatch, lambda request: _message())
    assert client.endpoints == ["beta"] and client.requests[0]["fallbacks"] == "default"
    client, _ = _send_with(monkeypatch, lambda request: _message(), _config(model="claude-sonnet-4-6"), caps=SONNET_46)
    assert client.endpoints == ["messages"]
