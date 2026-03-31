"""
GitHub API interactions: post review comment, set commit status, submit review event.
"""

import os
import requests

SEVERITY_EMOJI = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "suggestion": "💡",
}

SEVERITY_LABEL = {
    "critical": "Critical",
    "major": "Major",
    "minor": "Minor",
    "suggestion": "Suggestion",
}


def post_review_comment(body: str) -> None:
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    _gh_post(url, {"body": body})


def set_commit_status(state: str, description: str) -> None:
    """state: 'success' | 'failure' | 'pending'"""
    repo = os.environ["REPO"]
    sha = os.environ["HEAD_SHA"]
    url = f"https://api.github.com/repos/{repo}/statuses/{sha}"
    _gh_post(url, {
        "state": state,
        "description": description[:140],  # GitHub limit
        "context": "Paul / AI PR Review",
    })


def submit_review(event: str, body: str) -> None:
    """event: 'APPROVE' | 'REQUEST_CHANGES'"""
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    _gh_post(url, {"event": event, "body": body})


def format_comment(result: dict, config: dict) -> str:
    issues = result.get("issues", [])
    changes = result.get("changes", [])
    summary = result.get("summary", "")
    model = config.get("model", "unknown model")

    counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
    for issue in issues:
        sev = issue.get("severity", "suggestion")
        counts[sev] = counts.get(sev, 0) + 1

    lines = ["## 👨‍⚖️ Paul's Review", ""]

    # ── Walkthrough (collapsible) ────────────────────────────────────────────
    lines += [
        "<details>",
        "<summary>📋 Walkthrough</summary>",
        "",
        f"{summary}",
        "",
    ]

    if changes:
        lines += [
            "**Changes**",
            "",
            "| File | Summary |",
            "|------|---------|",
        ]
        for change in changes:
            f = change.get("file", "")
            s = change.get("summary", "")
            lines.append(f"| `{f}` | {s} |")
        lines.append("")

    lines += [
        "**Severity Overview**",
        "",
        "| Severity | Count |",
        "|----------|-------|",
        f"| 🔴 Critical | {counts['critical']} |",
        f"| 🟠 Major | {counts['major']} |",
        f"| 🟡 Minor | {counts['minor']} |",
        f"| 💡 Suggestion | {counts['suggestion']} |",
        "",
        "</details>",
        "",
        "---",
    ]

    # ── Individual issues ────────────────────────────────────────────────────
    for issue in issues:
        lines.extend(_format_issue(issue))

    # ── Test recommendations ─────────────────────────────────────────────────
    test_recs = result.get("test_recommendations", [])
    if test_recs:
        lines += ["", "### 🧪 Test Recommendations", ""]
        for rec in test_recs:
            lines.append(f"- {rec}")

    # ── Combined AI agent prompt (collapsible) ───────────────────────────────
    if issues:
        lines += ["", "---", ""]
        lines += _format_combined_agent_prompt(issues)

    lines += [
        "",
        "---",
        f"*Powered by [Paul](https://github.com/gmarte/PaulRudd) · Model: `{model}`*",
    ]

    return "\n".join(lines)


def _format_issue(issue: dict) -> list:
    sev = issue.get("severity", "suggestion")
    emoji = SEVERITY_EMOJI.get(sev, "💡")
    label = SEVERITY_LABEL.get(sev, sev.title())
    file_ref = issue.get("file", "unknown")
    line_start = issue.get("line_start")
    line_end = issue.get("line_end")

    if line_start and line_end and line_start != line_end:
        location = f"`{file_ref}:{line_start}-{line_end}`"
    elif line_start:
        location = f"`{file_ref}:{line_start}`"
    else:
        location = f"`{file_ref}`"

    title = issue.get("title", "")
    description = issue.get("description", "")
    impact = issue.get("impact", "")
    suggestion = issue.get("suggestion", {})
    explanation = suggestion.get("explanation", "")

    summary_line = f"{emoji} [{label}] {title} — {location}"

    body = []
    if impact:
        body.append(f"**Impact:** {impact}")
    body.append(f"**Description:** {description}")
    if explanation:
        body.append(f"**Fix:** {explanation}")

    lines = [
        "",
        "<details>",
        f"<summary>{summary_line}</summary>",
        "",
    ] + body + [
        "",
        "</details>",
    ]

    return lines


def _format_combined_agent_prompt(issues: list) -> list:
    """Single collapsible block with a ready-to-paste prompt covering all issues."""
    prompt_lines = [
        "You are fixing issues flagged by Paul, an AI PR reviewer.",
        "Please fix each of the following issues in the codebase:",
        "",
    ]

    for i, issue in enumerate(issues, 1):
        file_ref = issue.get("file", "unknown")
        line_start = issue.get("line_start")
        line_end = issue.get("line_end")
        title = issue.get("title", "")
        description = issue.get("description", "")
        suggestion = issue.get("suggestion", {})
        explanation = suggestion.get("explanation", "")
        autofix = suggestion.get("autofix") or {}
        original = autofix.get("original", "")
        replacement = autofix.get("replacement", "")

        if line_start and line_end and line_start != line_end:
            line_ref = f"lines {line_start}–{line_end}"
        elif line_start:
            line_ref = f"line {line_start}"
        else:
            line_ref = "the affected area"

        prompt_lines += [f"── Issue {i}: {title}", f"   File: {file_ref} ({line_ref})"]
        if description:
            prompt_lines.append(f"   Problem: {description}")
        if explanation:
            prompt_lines.append(f"   How to fix: {explanation}")
        if original and replacement:
            prompt_lines += [
                "   Replace this:",
                f"   {original}",
                "   With this:",
                f"   {replacement}",
            ]
        prompt_lines.append("")

    prompt_lines.append(
        "After all fixes are applied, run the existing test suite and flag any tests "
        "that need updating. Add new tests where noted."
    )

    combined = "\n".join(prompt_lines)

    return [
        "<details>",
        "<summary>🤖 Prompt for all issues — paste into Claude Code or Cursor to fix everything at once</summary>",
        "",
        "```",
        combined,
        "```",
        "",
        "</details>",
    ]


def _gh_post(url: str, payload: dict) -> requests.Response:
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response
