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
    overall = result.get("overall_severity", "suggestion")
    summary = result.get("summary", "")
    model = config.get("model", "unknown model")

    counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
    for issue in issues:
        sev = issue.get("severity", "suggestion")
        counts[sev] = counts.get(sev, 0) + 1

    lines = [
        "## 👨‍⚖️ Paul's Review",
        "",
        f"**Summary:** {summary}",
        "",
        "| Severity | Count |",
        "|----------|-------|",
        f"| 🔴 Critical | {counts['critical']} |",
        f"| 🟠 Major | {counts['major']} |",
        f"| 🟡 Minor | {counts['minor']} |",
        f"| 💡 Suggestion | {counts['suggestion']} |",
    ]

    if issues:
        lines.append("")
        lines.append("---")
        for issue in issues:
            lines.extend(_format_issue(issue))

    test_recs = result.get("test_recommendations", [])
    if test_recs:
        lines.append("")
        lines.append("### 🧪 Test Recommendations")
        for rec in test_recs:
            lines.append(f"- {rec}")

    lines.extend([
        "",
        "---",
        f"*Powered by [Paul](https://github.com/giancarlopro/paul) · Model: `{model}`*",
    ])

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
    code = suggestion.get("code", "")

    lines = [
        "",
        f"### {emoji} [{label}] {title} — {location}",
    ]
    if impact:
        lines.append(f"**Impact:** {impact}")
    lines.append(f"**Description:** {description}")
    if explanation:
        lines.append(f"**Fix:** {explanation}")
    if code:
        lines.append(code)

    return lines


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
