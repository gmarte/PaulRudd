"""
The coverage ledger: what happened to every changed file in the PR.

A file is either reviewed, skipped by design (excluded, deleted, binary), or
failed. A failed file had changes that nobody reviewed, so it makes the review
incomplete, and an incomplete review never passes the gate unless
on_incomplete is neutral and every failure is one it excuses.
"""

from collections import Counter

# Skipped by design: there is nothing for the per-file review to look at.
SKIP_LABELS = {
    "excluded": "matches excluded_paths",
    "removed": "deleted",
    "binary": "binary file",
    "renamed": "renamed without content changes",
    "no_changes": "no content changes",
}

# Failed: the file has changes that were not reviewed.
FAIL_LABELS = {
    "too_large": "diff too large for the GitHub API",
    "over_file_limit": "beyond the 3,000 files the GitHub API returns",
    "context_exceeded": "diff too large for the model's context window",
    "llm_unavailable": "LLM unavailable after retries",
    "truncated": "LLM response hit max_tokens",
    "invalid_output": "LLM returned unusable output twice",
    "refused": "LLM declined to review it",
    "llm_rejected": "LLM rejected the request",
    "time_budget": "time_budget_minutes ran out before this file",
    "budget": "budget limit reached (budget.max_files or budget.max_cost_usd)",
    "internal_error": "Paul hit an unexpected error (see the job log)",
}

# The only failures on_incomplete: neutral excuses: the provider stayed down. A PR's
# author could cause any other (huge or padded diffs, content that makes the model
# refuse or answer badly) to keep a file away from review.
NEUTRAL_REASONS = {"llm_unavailable"}


class Coverage:
    def __init__(self):
        self.reviewed = []
        self.skipped = []   # (path, reason)
        self.failed = []    # (path, reason)
        self.unlisted = []  # (count, reason): failures GitHub didn't name, such as files past its API limit

    def review(self, path: str) -> None:
        self.reviewed.append(path)

    def skip(self, path: str, reason: str) -> None:
        self.skipped.append((path, reason))

    def fail(self, path: str, reason: str) -> None:
        self.failed.append((path, reason))

    def fail_unlisted(self, count: int, reason: str) -> None:
        self.unlisted.append((count, reason))

    @property
    def failed_count(self) -> int:
        return len(self.failed) + sum(count for count, _ in self.unlisted)

    @property
    def failed_paths(self) -> set:
        return {path for path, _ in self.failed}

    @property
    def complete(self) -> bool:
        return self.failed_count == 0

    @property
    def excusable(self) -> bool:
        """True when every unreviewed file failed for a reason on_incomplete: neutral covers."""
        return not self.unlisted and all(reason in NEUTRAL_REASONS for _, reason in self.failed)

    def skip_counts(self) -> Counter:
        return Counter(reason for _, reason in self.skipped)
