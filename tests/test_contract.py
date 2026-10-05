"""
Contract tests: the real SDK call paths (Anthropic SDK for Claude, LiteLLM for
OpenAI and Gemini) against a local server, pinning down what goes over the wire:
cache markers, structured outputs, effort, refusal fallbacks, temperature, and
how a stream that fails part-way is handled.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import llm as llm_core
import llm_anthropic
import reviewer
from schemas import ENVELOPE

REVIEW = json.dumps({"task": "review", "walkthrough": None, "verification": None,
                     "review": {"issues": [], "test_recommendations": [], "resolved_prior_findings": []}})
CAPABILITIES = {
    "claude-sonnet-5-5": {"structured_outputs": {"supported": True},
                          "effort": {"supported": True, **{lvl: {"supported": True}
                                                         for lvl in ("low", "medium", "high", "xhigh", "max")}}},
    "claude-sonnet-4-6": {"structured_outputs": {"supported": False},
                          "effort": {"supported": True, **{lvl: {"supported": lvl != "xhigh"}
                                                         for lvl in ("low", "medium", "high", "xhigh", "max")}}},
}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def _text_block(index, text):
    return [
        _sse("content_block_start", {"type": "content_block_start", "index": index,
                                     "content_block": {"type": "text", "text": ""}}),
        _sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                     "delta": {"type": "text_delta", "text": text}}),
        _sse("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


class _Server(BaseHTTPRequestHandler):
    captured = []
    models_status = 200
    # What the next streamed answers do, in order; "ok" once the list runs out.
    scenarios = []

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        out = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.startswith("/v1/models/"):
            model = self.path.split("/v1/models/", 1)[1].split("?")[0]
            if _Server.models_status != 200:
                return self._send({"type": "error", "error": {"type": "api_error", "message": "down"}},
                                  _Server.models_status)
            return self._send({"id": model, "type": "model", "display_name": model,
                               "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1000000,
                               "max_tokens": 128000, "capabilities": CAPABILITIES.get(model, {})})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        _Server.captured.append((self.path, dict(self.headers), body))
        if "generateContent" in self.path:
            reply = {"candidates": [{"content": {"parts": [{"text": REVIEW}], "role": "model"},
                                     "finishReason": "STOP", "index": 0}],
                     "usageMetadata": {"promptTokenCount": 2000, "candidatesTokenCount": 20, "totalTokenCount": 2020}}
        elif self.path.startswith("/v1/messages"):
            assert body.get("stream") is True, "every Claude call streams"
            return self._stream(body["model"], _Server.scenarios.pop(0) if _Server.scenarios else "ok")
        else:
            reply = {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": REVIEW},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 2000, "completion_tokens": 20, "total_tokens": 2020}}
        self._send(reply)

    def _stream(self, model, scenario):
        start = _sse("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 300, "output_tokens": 1, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 1700}}})
        end = [_sse("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": 20}}),
               _sse("message_stop", {"type": "message_stop"})]
        if scenario == "ok":
            chunks = [start, *_text_block(0, REVIEW), *end]
        elif scenario == "fallback":
            # Declined part-way: the partial answer stays, a marker, then the fallback model continues.
            chunks = [start, *_text_block(0, REVIEW[:30]),
                      _sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                          "type": "fallback", "from": {"model": model}, "to": {"model": "claude-opus-5-5"}}}),
                      _sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
                      *_text_block(2, REVIEW[30:]), *end]
        elif scenario == "overloaded":
            chunks = [start, _sse("error", {"type": "error",
                                            "error": {"type": "overloaded_error", "message": "Overloaded"}})]
        else:  # "drop": the connection closes part-way through the answer
            chunks = [start, *_text_block(0, REVIEW[:30])[:2]]
        payload = b"".join(chunks)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload) + (5000 if scenario == "drop" else 0)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        if scenario == "drop":
            self.connection.shutdown(socket.SHUT_RDWR)


@pytest.fixture
def wire(monkeypatch):
    _Server.captured, _Server.models_status, _Server.scenarios = [], 200, []
    server = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_BASE", f"{base}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_BASE", base)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield _Server
    server.shutdown()


def _config(provider, model, temperature=None, ttl="5m"):
    return {"provider": provider, "model": model, "max_tokens": 4096, "temperature": temperature, "effort": "medium",
            "repo_context": "### CLAUDE.md\n\nUse services.", "custom_instructions": "", "language": "",
            "cache": {"ttl": ttl}, "budget": {"max_cost_usd": 5.0}}


def _review(config):
    plan = reviewer.build_plan(config, "<pr>PR</pr>").with_task("TASK: review\n\nReview only this file: a.py")
    return reviewer.review_file("a.py", plan, config)


def test_claude_5_request_on_the_wire(wire):
    assert _review(_config("anthropic", "claude-sonnet-5-5", temperature=0))["issues"] == []
    path, headers, body = wire.captured[-1]
    assert path.startswith("/v1/messages")
    assert [block["cache_control"] for block in body["system"]] == [{"type": "ephemeral"}] * 2
    assert "Precision Rules" in body["system"][0]["text"] and "Use services." in body["system"][1]["text"]
    pr_block, task_block = body["messages"][0]["content"]
    assert pr_block == {"type": "text", "text": "<pr>PR</pr>", "cache_control": {"type": "ephemeral"}}
    assert "cache_control" not in task_block
    assert body["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": ENVELOPE}}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert "temperature" not in body
    assert body["max_tokens"] == 16000  # room to think, whatever max_tokens says
    assert llm_core.USAGE["cache_read_tokens"] == 1700 and llm_core.USAGE["output_tokens"] == 20


def test_sonnet_4_6_request_has_no_schema_but_keeps_temperature(wire):
    _review(_config("anthropic", "claude-sonnet-4-6", temperature=0))
    _, headers, body = wire.captured[-1]
    assert body["output_config"] == {"effort": "medium"}
    assert body["temperature"] == 0 and body["max_tokens"] == 4096
    assert "fallbacks" not in body and "anthropic-beta" not in {k.lower() for k in headers}


def test_one_hour_ttl_on_the_wire(wire):
    _review(_config("anthropic", "claude-sonnet-5-5", ttl="1h"))
    _, _, body = wire.captured[-1]
    assert [block["cache_control"] for block in body["system"]] == [{"type": "ephemeral", "ttl": "1h"}] * 2
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_an_unreachable_models_api_falls_back_to_documented_capabilities(wire):
    wire.models_status = 500
    _review(_config("anthropic", "claude-sonnet-5-5"))
    _, _, body = wire.captured[-1]
    assert body["output_config"]["format"]["type"] == "json_schema"


def test_an_answer_split_by_a_refusal_fallback_is_read_whole(wire):
    wire.scenarios = ["fallback"]
    assert _review(_config("anthropic", "claude-sonnet-5-5"))["issues"] == []
    assert len(wire.captured) == 1  # no second call to make up for a broken answer


@pytest.mark.parametrize("scenario", ["overloaded", "drop"])
def test_a_stream_that_fails_part_way_is_retried(wire, scenario):
    # Review findings: a dropped stream used to crash the run, and an overload part-way failed the file.
    wire.scenarios = [scenario]
    assert _review(_config("anthropic", "claude-sonnet-5-5"))["issues"] == []
    assert len(wire.captured) == 2


def test_openai_request_on_the_wire(wire):
    _review(_config("openai", "gpt-4o"))
    path, _, body = wire.captured[-1]
    assert path.endswith("/chat/completions")
    assert body["messages"][0]["role"] == "system" and "Precision Rules" in body["messages"][0]["content"]
    assert body["response_format"]["type"] in ("json_schema", "json_object")
    assert "cache_control" not in json.dumps(body)
    assert body.get("prompt_cache_key", "").startswith("paul:")


def test_gemini_request_on_the_wire(wire):
    _review(_config("google", "gemini-2.5-pro"))
    path, _, body = wire.captured[-1]
    assert "generateContent" in path
    system = body.get("system_instruction") or body.get("systemInstruction")
    assert system and "Precision Rules" in json.dumps(system)
    assert "cachedContent" not in body and "cache_control" not in json.dumps(body)


def test_the_transport_seam_is_what_the_pipeline_fakes():
    assert callable(llm_anthropic.send)
