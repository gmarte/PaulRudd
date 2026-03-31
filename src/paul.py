"""
Paul — AI PR Review Bot entry point.
Orchestrates: load config → fetch diff → call LLM → post results to GitHub.
"""

import sys

from config import load_config
from diff_processor import fetch_diff
from github_client import format_comment, post_review_comment, set_commit_status, submit_review
from reviewer import determines_outcome, review


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
        set_commit_status("success", "No reviewable changes found.")
        submit_review("APPROVE", "No reviewable changes found (empty diff).")
        return

    print("Calling LLM for review...")
    result = review(diff, config)
    overall = result["overall_severity"]
    issue_count = len(result.get("issues", []))
    print(f"  Overall severity: {overall} | Issues found: {issue_count}")

    comment_body = format_comment(result, config)
    print("Posting review comment...")
    post_review_comment(comment_body)

    outcome = determines_outcome(overall, config["severity_threshold"])

    # Use a short review body — full details are already in the PR comment above.
    review_body = (
        f"Paul found {issue_count} issue(s). Highest severity: {overall}. "
        f"See the review comment above for details."
    )

    if outcome == "block":
        print(f"Blocking PR (severity '{overall}' meets threshold '{config['severity_threshold']}').")
        set_commit_status(
            "failure",
            f"Paul found {issue_count} issue(s) — highest severity: {overall}.",
        )
        submit_review("REQUEST_CHANGES", review_body)
        sys.exit(1)
    else:
        print(f"Approving PR (severity '{overall}' is below threshold '{config['severity_threshold']}').")
        set_commit_status(
            "success",
            f"Paul reviewed {issue_count} issue(s) — highest severity: {overall}.",
        )
        submit_review("APPROVE", review_body)


if __name__ == "__main__":
    main()
