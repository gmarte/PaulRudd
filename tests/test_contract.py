"""
Contract tests: Paul's real LiteLLM call path against a local server, pinning
down what goes over the wire for each provider (system prompt placement, prompt
caching markers, temperature).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import reviewer

REVIEW = json.dumps({"issues": [], "test_recommendations": [], "resolved_prior_findings": []})


class _Capture(BaseHTTPRequestHandler):
    captured = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        _Capture.captured.append((self.path, body))
        if "generateContent" in self.path:
            reply = {
                "candidates": [{"content": {"parts": [{"text": REVIEW}], "role": "model"}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 2000, "candidatesTokenCount": 20, "totalTokenCount": 2020},
            }
        elif self.path.endswith("/messages"):
            reply = {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": REVIEW}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 300, "output_tokens": 20, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 1700},
            }
        else:
            reply = {
                "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REVIEW}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2000, "completion_tokens": 20, "total_tokens": 2020},
            }
        out = json.dumps(reply).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def wire(monkeypatch):
    _Capture.captured = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_API_BASE", base)
    monkeypatch.setenv("OPENAI_API_BASE", f"{base}/v1")
    monkeypatch.setenv("GEMINI_API_BASE", base)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield _Capture.captured
    server.shutdown()


def _config(provider, model, temperature=None):
    return {"provider": provider, "model": model, "max_tokens": 4096, "temperature": temperature,
            "repo_context": "", "custom_instructions": "", "language": ""}


def test_anthropic_gets_a_cached_system_block_and_temperature(wire):
    reviewer.review_file("a.py", "Review only this file: a.py", _config("anthropic", "claude-sonnet-4-6", temperature=0))
    path, body = wire[-1]
    assert path.endswith("/messages")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Severity Model" in body["system"][0]["text"]
    assert body["messages"][0]["role"] == "user"
    assert body["temperature"] == 0
    assert reviewer.USAGE["cache_read_tokens"] == 1700


def test_claude_5_models_never_receive_temperature(wire):
    reviewer.review_file("a.py", "Review only this file: a.py", _config("anthropic", "claude-sonnet-5-5", temperature=0))
    _, body = wire[-1]
    assert "temperature" not in body
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_openai_gets_the_system_prompt_as_a_system_message(wire):
    reviewer.review_file("a.py", "Review only this file: a.py", _config("openai", "gpt-4o"))
    path, body = wire[-1]
    assert path.endswith("/chat/completions")
    assert body["messages"][0]["role"] == "system"
    assert "Severity Model" in json.dumps(body["messages"][0]["content"])
    assert "cache_control" not in json.dumps(body)
    assert body["response_format"] == {"type": "json_object"}


def test_gemini_gets_a_system_instruction_and_no_explicit_cache(wire):
    reviewer.review_file("a.py", "Review only this file: a.py", _config("google", "gemini-2.5-pro"))
    path, body = wire[-1]
    assert "generateContent" in path
    system = body.get("system_instruction") or body.get("systemInstruction")
    assert system and "Severity Model" in json.dumps(system)
    assert "cachedContent" not in body and "cache_control" not in json.dumps(body)
