"""
Paul — AI PR Review Bot entry point.

Two-pass orchestration:
  Pass 1 → Walkthrough (summary + changes) → post comment immediately
  Pass 2 → Issues per file                 → edit comment with full review
"""

import sys

import litellm

from config import load_config
from diff_processor import fetch_diff, split_into_file_blocks
from github_client import (
    edit_comment,
    format_comment,
    format_walkthrough,
    post_walkthrough_comment,
    submit_review,
)
from reviewer import SEVERITY_ORDER, determines_outcome, review_file, review_walkthrough


def main() -> None:
    print("Paul is on the case...")

    config = load_config()
    print(f"  Provider: {config['provider']} | Model: {config['model']}")
    print(f"  Severity threshold: {config['severity_threshold']}")

    print("Fetching PR diff...")
    diff = fetch_diff(config)
    print(f"  Diff size: {len(diff):,} chars")

    if not diff.strip():
        print("Empty diff — nothing to review. Approving.")
        submit_review("APPROVE", "No reviewable changes found (empty diff).")
        return

    # ── Pass 1: Walkthrough ──────────────────────────────────────────────────
    print("Pass 1: Generating walkthrough...")
    try:
        walkthrough = review_walkthrough(diff, config)
    except (litellm.exceptions.InternalServerError, litellm.exceptions.ServiceUnavailableError):
        print("API unavailable after all retries — skipping review.")
        sys.exit(0)

    print("  Posting walkthrough comment...")
    comment_id = post_walkthrough_comment(format_walkthrough(walkthrough, config))
    print(f"  Comment posted (id={comment_id}). Reviewers can see the walkthrough now.")

    # ── Pass 2: Issues per file ──────────────────────────────────────────────
    file_blocks = split_into_file_blocks(diff)
    print(f"Pass 2: Reviewing {len(file_blocks)} file(s) for issues...")

    all_issues = []
    all_test_recs = []

    for file_path, file_diff in file_blocks:
        print(f"  → {file_path}")
        try:
            result = review_file(file_path, file_diff, config)
        except (litellm.exceptions.InternalServerError, litellm.exceptions.ServiceUnavailableError):
            print(f"    API unavailable for {file_path} — skipping file.")
            continue
        except ValueError as e:
            print(f"    Skipping {file_path}: {e}")
            continue
        all_issues.extend(result.get("issues", []))
        all_test_recs.extend(result.get("test_recommendations", []))

    # Recompute overall_severity from actual issues found (more accurate than Pass 1 estimate)
    if all_issues:
        max_issue = max(
            all_issues,
            key=lambda i: SEVERITY_ORDER.index(i.get("severity", "suggestion")),
        )
        overall_severity = max_issue.get("severity", "suggestion")
    else:
        overall_severity = "suggestion"

    full_result = {
        "overall_severity": overall_severity,
        "summary": walkthrough["summary"],
        "changes": walkthrough["changes"],
        "issues": all_issues,
        "test_recommendations": all_test_recs,
    }

    issue_count = len(all_issues)
    print(f"  Overall severity: {overall_severity} | Issues found: {issue_count}")

    # ── Update comment with full review ──────────────────────────────────────
    print("Updating comment with full review...")
    edit_comment(comment_id, format_comment(full_result, config))

    # ── Submit formal review ─────────────────────────────────────────────────
    outcome = determines_outcome(overall_severity, config["severity_threshold"])
    review_body = (
        f"Paul found {issue_count} issue(s). Highest severity: {overall_severity}. "
        f"See the review comment for details."
    )

    if outcome == "block":
        print(f"Blocking PR (severity '{overall_severity}' meets threshold '{config['severity_threshold']}').")
        submit_review("REQUEST_CHANGES", review_body)
        sys.exit(1)  # Non-zero exit makes the workflow job fail → blocks the PR
    else:
        print(f"Approving PR (severity '{overall_severity}' is below threshold '{config['severity_threshold']}').")
        submit_review("APPROVE", review_body)


if __name__ == "__main__":
    main()
