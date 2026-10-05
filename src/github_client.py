"""
GitHub API interactions: read the PR's files and files at its base commit,
maintain Paul's summary comment on the PR, and submit the formal review.

Every call goes through request(), which targets GITHUB_API_URL (so GitHub
Enterprise Server works) and retries server errors and rate limits.
"""

import json
import os
import re
import time
from urllib.parse import quote

import requests

API_VERSION = "2022-11-28"
SUMMARY_MARKER = "<!-- paul:summary -->"
REVIEW_PREFIX = "Paul found"      # every formal review body Paul submits starts with this
MAX_ATTEMPTS = 4
MAX_RETRY_WAIT = 300              # seconds; a longer wait fails the request instead

# Paul's summary comment always ends with its two markers (see render._footer).
# Requiring them at the very end means a comment that merely quotes them (another
# bot echoing a PR title, say) isn't mistaken for Paul's.
_SUMMARY_FOOTER = re.compile(r"<!-- paul:summary -->\n<!-- paul:findings [A-Za-z0-9+/=]* -->\s*\Z")
# Summary comments posted by Paul versions before the markers existed.
_LEGACY_SIGNATURE = ("## Paul's Review", "*Powered by [Paul](https://github.com/gmarte/PaulRudd)")
_IDEMPOTENT = {"GET", "PATCH", "PUT"}

_session = requests.Session()


# ── HTTP ─────────────────────────────────────────────────────────────────────

def api_url(path: str) -> str:
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def request(method: str, url: str, accept: str = "application/vnd.github+json", max_wait: float = MAX_RETRY_WAIT,
            **kwargs) -> requests.Response:
    """
    Send a GitHub API request, retrying 5xx responses, rate limits and dropped
    connections. A retry that would mean waiting more than max_wait seconds fails instead.
    """
    headers = {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _session.request(method, url, headers=headers, timeout=30, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            # A POST may have gone through before the connection dropped; retrying
            # it could post twice, so only idempotent requests are retried.
            if method not in _IDEMPOTENT or attempt == MAX_ATTEMPTS:
                raise
            print(f"  GitHub API connection failed ({type(e).__name__}); retrying in {2 ** attempt}s...")
            time.sleep(2 ** attempt)
            continue
        delay = _retry_delay(response, attempt, max_wait)
        if delay is None or attempt == MAX_ATTEMPTS:
            break
        print(f"  GitHub API returned {response.status_code}; retrying in {delay:.0f}s...")
        time.sleep(delay)
    response.raise_for_status()
    return response


def _retry_delay(response: requests.Response, attempt: int, max_wait: float = MAX_RETRY_WAIT) -> float | None:
    """
    Seconds to wait before retrying, or None if the response shouldn't be retried.
    Follows GitHub's guidance: honour retry-after, wait for x-ratelimit-reset when
    the limit is used up, and otherwise wait at least a minute after a rate limit.
    """
    status = response.status_code
    headers = response.headers
    rate_limited = status == 429 or (status == 403 and (
        headers.get("retry-after")
        or headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in response.text.lower()
    ))
    if status not in (500, 502, 503, 504) and not rate_limited:
        return None

    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        wait = float(retry_after)
    elif rate_limited and headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset", "").isdigit():
        wait = max(float(headers["x-ratelimit-reset"]) - time.time(), 1.0)
    elif rate_limited:
        wait = 60.0
    else:
        wait = float(2 ** attempt)
    # Retrying before GitHub allows it would only extend the limit; give up instead.
    return wait if wait <= max_wait else None


def get_paginated(url: str, params: dict | None = None) -> list:
    """Every item of a list endpoint, following the Link headers 100 items at a time."""
    items = []
    params = {**(params or {}), "per_page": 100}
    while url:
        response = request("GET", url, params=params)
        items.extend(response.json())
        url = response.links.get("next", {}).get("url")
        params = None  # the next link already carries the query string
    return items


def _repo() -> str:
    return os.environ["REPO"]


def _pr_number() -> str:
    return os.environ["PR_NUMBER"]


def load_event() -> dict:
    """The webhook payload of the triggering event, or {} outside GitHub Actions."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Reading ──────────────────────────────────────────────────────────────────

def list_pr_files() -> list:
    """The PR's changed files with their patches. GitHub returns at most 3,000."""
    return get_paginated(api_url(f"repos/{_repo()}/pulls/{_pr_number()}/files"))


def get_file_at(path: str, ref: str, max_wait: float = MAX_RETRY_WAIT) -> str | None:
    """A file's text at a commit, or None if it doesn't exist there."""
    url = api_url(f"repos/{_repo()}/contents/{quote(path)}")
    try:
        response = request("GET", url, accept="application/vnd.github.raw+json", params={"ref": ref},
                           max_wait=max_wait)
    except requests.HTTPError as e:
        if _status(e) == 404:
            return None
        raise
    return response.content.decode("utf-8", errors="replace")


def list_dir_at(path: str, ref: str) -> list:
    """Names of the files directly inside a directory at a commit ([] if it doesn't exist)."""
    url = api_url(f"repos/{_repo()}/contents/{quote(path)}")
    try:
        entries = request("GET", url, params={"ref": ref}).json()
    except requests.HTTPError as e:
        if _status(e) == 404:
            return []
        raise
    if not isinstance(entries, list):
        return []
    return [entry["name"] for entry in entries if entry.get("type") == "file"]


def _status(error: requests.HTTPError) -> int | None:
    return error.response.status_code if error.response is not None else None


# ── Summary comment ──────────────────────────────────────────────────────────

def find_summary_comment() -> dict | None:
    """
    Paul's most recent summary comment on this PR as {"id", "body"}, or None.

    Only comments written by a bot count, so nobody can plant a fake review
    that Paul would then treat as its own history. The body must also end with
    Paul's markers (or carry the signature of a pre-marker Paul comment).
    """
    comments = get_paginated(api_url(f"repos/{_repo()}/issues/{_pr_number()}/comments"))
    for comment in reversed(comments):
        body = comment.get("body") or ""
        author = comment.get("user") or {}
        if author.get("type") != "Bot":
            continue
        if _SUMMARY_FOOTER.search(body) or all(part in body for part in _LEGACY_SIGNATURE):
            return {"id": comment["id"], "body": body}
    return None


def upsert_summary_comment(body: str, comment_id: int | None = None, fallback_body: str | None = None) -> int:
    """Create Paul's summary comment, or replace it in place. Returns the comment ID."""
    try:
        return _write_comment(body, comment_id)
    except requests.HTTPError as e:
        # 422: GitHub rejected the body, for example for being over 65,536 characters.
        if fallback_body is None or _status(e) != 422:
            raise
        print("  GitHub rejected the comment body; posting the short version instead.")
        return _write_comment(fallback_body, comment_id)


def _write_comment(body: str, comment_id: int | None) -> int:
    if comment_id:
        try:
            request("PATCH", api_url(f"repos/{_repo()}/issues/comments/{comment_id}"), json={"body": body})
            return comment_id
        except requests.HTTPError as e:
            # 404: the comment was deleted. 403: it belongs to another app.
            if _status(e) not in (403, 404):
                raise
            print(f"  Can't edit Paul's earlier comment ({_status(e)}); posting a new one.")
    response = request("POST", api_url(f"repos/{_repo()}/issues/{_pr_number()}/comments"), json={"body": body})
    return response.json()["id"]


# ── Reviews ──────────────────────────────────────────────────────────────────

def submit_review(body: str) -> None:
    """Submit a COMMENT review. Paul never approves; the job's exit code is the merge gate."""
    payload = {"event": "COMMENT", "body": body}
    head_sha = os.environ.get("HEAD_SHA")
    if head_sha:
        payload["commit_id"] = head_sha
    try:
        request("POST", api_url(f"repos/{_repo()}/pulls/{_pr_number()}/reviews"), json=payload)
    except requests.RequestException as e:
        print(f"::warning::Could not submit the review ({e}). The job's exit code still enforces the gate.")


def dismiss_stale_change_requests() -> None:
    """Dismiss REQUEST_CHANGES reviews left by earlier Paul versions, which would keep blocking the PR."""
    url = api_url(f"repos/{_repo()}/pulls/{_pr_number()}/reviews")
    try:
        for review in get_paginated(url):
            author = review.get("user") or {}
            if (
                review.get("state") == "CHANGES_REQUESTED"
                and author.get("type") == "Bot"
                and (review.get("body") or "").startswith(REVIEW_PREFIX)
            ):
                request("PUT", f"{url}/{review['id']}/dismissals", json={
                    "message": "Superseded by Paul's latest review, which found no blocking issues.",
                    "event": "DISMISS",
                })
                print(f"  Dismissed Paul's earlier change request (review {review['id']}).")
    except requests.RequestException as e:
        print(f"::warning::Could not dismiss Paul's earlier change requests ({e}).")
