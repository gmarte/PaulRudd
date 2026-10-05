"""
Shared fixtures: an in-memory GitHub API, a scripted stand-in for the Claude
transport (llm_anthropic.send), and a clean environment for every test.
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import github_client  # noqa: E402
import llm as llm_core  # noqa: E402  (`llm` is the fake's fixture name)
import llm_anthropic  # noqa: E402

REPO = "acme/shop"
PR = "7"
BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40

SQL_PATCH = (
    "@@ -10,3 +10,5 @@ def search(request):\n"
    "     qs = Invoice.objects.all()\n"
    "+    rnc = request.GET['rnc']\n"
    "+    rows = cursor.execute(f\"SELECT * FROM invoices WHERE rnc = {rnc}\")\n"
    "     return render(request, 'list.html')\n"
)

SQL_FINDING = {
    "severity": "critical",
    "line_start": 12,
    "line_end": 12,
    "title": "SQL injection in invoice search",
    "description": "The RNC from the query string is interpolated into SQL.",
    "impact": "Anyone can read or change the invoices table.",
    "suggestion": {"explanation": "Pass rnc as a query parameter.", "autofix": None},
}

_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")


# ── GitHub ───────────────────────────────────────────────────────────────────

def make_response(status=200, json_body=None, text=None, headers=None, url="https://api.github.com/"):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers = requests.structures.CaseInsensitiveDict(headers or {})
    payload = json.dumps(json_body) if json_body is not None else (text or "")
    response._content = payload.encode("utf-8")
    return response


def pr_file(path, patch=None, status="modified", additions=None, deletions=None):
    lines = (patch or "").splitlines()
    entry = {
        "filename": path,
        "status": status,
        "additions": additions if additions is not None else sum(1 for l in lines if l.startswith("+")),
        "deletions": deletions if deletions is not None else sum(1 for l in lines if l.startswith("-")),
    }
    if patch is not None:
        entry["patch"] = patch
    return entry


class FakeGitHub:
    """An in-memory GitHub API for one repo and one PR."""

    def __init__(self):
        self.pr_files = []
        self.base_files = {}       # path -> text at BASE_SHA
        self.head_files = {}       # path -> text at HEAD_SHA
        self.comments = []         # {"id", "body", "user": {"type"}}
        self.reviews = []          # existing reviews on the PR
        self.submitted_reviews = []
        self.dismissed = []
        self.writes = []           # (method, comment_id, body), in order
        self.queued = {}           # (method, path) -> statuses returned before the real handler
        self.requests = []         # (method, url)
        self._next_id = 1000

    @property
    def last_body(self):
        return self.writes[-1][2] if self.writes else None

    def __call__(self, method, url, headers=None, timeout=None, params=None, json=None, **kwargs):
        self.requests.append((method, url))
        parsed = urlparse(url)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})

        queued = self.queued.get((method, path))
        if queued:
            return make_response(queued.pop(0), json_body={"message": "queued failure"}, url=url)

        repo = f"/repos/{REPO}"
        if method == "GET" and path == f"{repo}/pulls/{PR}/files":
            return self._page(self.pr_files, query, url)
        if method == "GET" and path.startswith(f"{repo}/contents/"):
            return self._contents(path[len(f"{repo}/contents/"):], query, url)
        if method == "GET" and path == f"{repo}/issues/{PR}/comments":
            return self._page(self.comments, query, url)
        if method == "POST" and path == f"{repo}/issues/{PR}/comments":
            return self._write_comment(None, json["body"], url)
        if method == "PATCH" and path.startswith(f"{repo}/issues/comments/"):
            return self._write_comment(int(path.rsplit("/", 1)[1]), json["body"], url)
        if method == "POST" and path == f"{repo}/pulls/{PR}/reviews":
            self.submitted_reviews.append(json)
            return make_response(200, json_body={"id": 1}, url=url)
        if method == "GET" and path == f"{repo}/pulls/{PR}/reviews":
            return self._page(self.reviews, query, url)
        if method == "PUT" and path.endswith("/dismissals"):
            self.dismissed.append(int(path.split("/")[-2]))
            return make_response(200, json_body={}, url=url)
        raise AssertionError(f"Unexpected GitHub call: {method} {url}")

    def _page(self, items, query, url):
        page, per_page = int(query.get("page", 1)), int(query.get("per_page", 30))
        headers = {}
        if page * per_page < len(items):
            headers["Link"] = f'<{url.split("?")[0]}?page={page + 1}&per_page={per_page}>; rel="next"'
        return make_response(200, json_body=items[(page - 1) * per_page: page * per_page], headers=headers, url=url)

    def _contents(self, path, query, url):
        ref = query.get("ref")
        assert ref in (BASE_SHA, HEAD_SHA), "files must be read at the base or head commit"
        files = self.base_files if ref == BASE_SHA else self.head_files
        if path in files:
            return make_response(200, text=files[path], url=url)
        prefix = path.rstrip("/") + "/"
        names = sorted(p[len(prefix):] for p in files if p.startswith(prefix) and "/" not in p[len(prefix):])
        if names:
            return make_response(200, json_body=[{"name": n, "type": "file"} for n in names], url=url)
        return make_response(404, json_body={"message": "Not Found"}, url=url)

    def _write_comment(self, comment_id, body, url):
        if len(body) > 65536:
            return make_response(422, json_body={"message": "body is too long (maximum is 65536 characters)"}, url=url)
        if comment_id is None:
            comment_id = self._next_id
            self._next_id += 1
            self.comments.append({"id": comment_id, "body": body, "user": {"type": "Bot", "login": "github-actions[bot]"}})
            self.writes.append(("POST", comment_id, body))
            return make_response(201, json_body={"id": comment_id}, url=url)
        existing = next((c for c in self.comments if c["id"] == comment_id), None)
        if existing is None:
            return make_response(404, json_body={"message": "Not Found"}, url=url)
        existing["body"] = body
        self.writes.append(("PATCH", comment_id, body))
        return make_response(200, json_body={"id": comment_id}, url=url)


# ── LLM ──────────────────────────────────────────────────────────────────────

def llm_response(content, finish_reason="stop", prompt_tokens=1200, cache_read=0, completion_tokens=150):
    """A transport reply (finish_reason uses the old names: stop, length, content_filter)."""
    stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish_reason, "end")
    fresh = max(prompt_tokens - cache_read, 0)
    return llm_core.Reply(content, stop, llm_core.Usage(fresh_input=fresh, cache_read=cache_read, output=completion_tokens,
                                                        cost_usd=0.001))


def review_json(*issues, resolved=(), test_recommendations=()):
    return json.dumps({
        "issues": list(issues),
        "test_recommendations": list(test_recommendations),
        "resolved_prior_findings": list(resolved),
    })


class FakeLLM:
    """
    Stands in for the Claude transport (llm_anthropic.send). Walkthrough calls are
    answered by `walkthrough(plan)`; file reviews by `review(label, plan)`, where
    label is the "Review only this file: ..." text. Either may return a reply or raise.
    """

    def __init__(self):
        self.calls = []  # the PromptPlan of every call
        self.walkthrough = lambda plan: llm_response(json.dumps({"summary": "Adds invoice search.", "changes": []}))
        self.review = lambda label, plan: llm_response(review_json())
        self._lock = threading.Lock()

    def __call__(self, plan, config, timeout):
        with self._lock:
            self.calls.append(plan)
        if plan.task.startswith("TASK: walkthrough"):
            return self.walkthrough(plan)
        return self.review(self.label(plan), plan)

    @staticmethod
    def label(plan):
        return re.search(r"Review only this file: (.+)", plan.task).group(1).strip()

    def review_labels(self):
        return [self.label(p) for p in self.calls if not p.task.startswith("TASK: walkthrough")]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for key in _KEY_VARS + ("PAUL_CONFIG_PATH", "RUNNER_DEBUG", "GITHUB_API_URL", "GITHUB_RUN_ID", "GITHUB_EVENT_NAME",
                            "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("PAUL_API_KEY", "sk-test")
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("PR_NUMBER", PR)
    monkeypatch.setenv("HEAD_SHA", HEAD_SHA)
    monkeypatch.setenv("BASE_SHA", BASE_SHA)
    monkeypatch.chdir(tmp_path)
    write_event(monkeypatch, tmp_path)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    llm_core.reset_usage()
    llm_core.set_deadline(None)
    llm_core.set_log_prefix("")
    llm_anthropic.capabilities.cache_clear()
    llm_anthropic._client.cache_clear()
    yield
    llm_core.set_deadline(None)
    llm_core.set_log_prefix("")


def write_event(monkeypatch, tmp_path, **pull_request):
    event = {
        "pull_request": {
            "number": int(PR),
            "title": "Add invoice search",
            "body": "Lets staff search invoices by RNC.",
            "changed_files": 0,
            "head": {"sha": HEAD_SHA, "repo": {"full_name": REPO}},
            "base": {"sha": BASE_SHA},
            **pull_request,
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_client._session, "request", fake)
    return fake


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_anthropic, "send", fake)
    return fake


def run_main():
    """Run paul.main() and return its exit code (0 when it returns normally)."""
    import paul
    try:
        paul.main()
    except SystemExit as e:
        return e.code
    return 0
