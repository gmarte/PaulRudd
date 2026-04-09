"""
Fetches the PR diff from the GitHub API, strips excluded paths,
and truncates to the character limit while preserving the most changed files.
"""

import fnmatch
import os
import re
import requests

MAX_DIFF_CHARS = 80_000


def fetch_diff(config: dict) -> str:
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    token = os.environ["GITHUB_TOKEN"]

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    raw_diff = response.text
    filtered = _filter_excluded_paths(raw_diff, config["excluded_paths"])
    truncated = _truncate(filtered)
    return truncated


def _filter_excluded_paths(diff: str, excluded_patterns: list) -> str:
    """Remove file blocks whose paths match any excluded glob pattern."""
    file_blocks = _split_into_file_blocks(diff)
    kept = []
    for path, block in file_blocks:
        if not any(_matches(path, pat) for pat in excluded_patterns):
            kept.append(block)
    return "\n".join(kept)


def _split_into_file_blocks(diff: str) -> list:
    """Split a unified diff into (file_path, block_text) tuples."""
    blocks = []
    current_path = None
    current_lines = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                blocks.append((current_path, "".join(current_lines)))
            current_path = _extract_path(line)
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_path is not None:
        blocks.append((current_path, "".join(current_lines)))

    return blocks


# Public alias used by reviewer.py for the two-pass pipeline
split_into_file_blocks = _split_into_file_blocks


def _extract_path(diff_header: str) -> str:
    """Extract 'b/src/foo.py' → 'src/foo.py' from a diff header line."""
    match = re.search(r" b/(.+)$", diff_header.strip())
    return match.group(1) if match else diff_header


def _matches(path: str, pattern: str) -> bool:
    """Check if path matches a glob pattern (handles ** correctly)."""
    # Normalize pattern: remove leading **/ for simple suffix matching
    clean = pattern.lstrip("*").lstrip("/")
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{clean}")


def _truncate(diff: str) -> str:
    """If diff exceeds MAX_DIFF_CHARS, keep the largest-changed files first."""
    if len(diff) <= MAX_DIFF_CHARS:
        return diff

    file_blocks = _split_into_file_blocks(diff)
    # Sort by block size descending so we keep the most-changed files
    file_blocks.sort(key=lambda x: len(x[1]), reverse=True)

    kept = []
    total = 0
    for path, block in file_blocks:
        if total + len(block) > MAX_DIFF_CHARS:
            break
        kept.append(block)
        total += len(block)

    truncation_notice = (
        f"\n\n[Diff truncated: showing {len(kept)} of {len(file_blocks)} files "
        f"({total:,} / {len(diff):,} chars). Largest changed files shown first.]\n"
    )
    return "\n".join(kept) + truncation_notice
