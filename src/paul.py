"""
Paul — AI PR Review Bot entry point.

  Preflight → no API key: skip fork and Dependabot PRs with a notice, fail any other
  Pass 1    → walkthrough (summary + changes) → summary comment posted immediately;
              this first call also writes the shared prompt prefix to the cache
  Pass 2    → issues per file, `concurrency` files at a time, each reading the cache
  Gate      → exit 1 on a finding at or above the threshold, or when a file with
              changes couldn't be reviewed (unless on_incomplete is neutral)

Every changed file lands in the coverage ledger: reviewed, skipped by design, or
failed. A failed file is never counted as clean.
"""

import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml

import github_client
import llm
import render
import reviewer
from config import is_trusted_path, load_config
from coverage import FAIL_LABELS, Coverage
from diff_processor import (
    PathFilter,
    annotate_patch,
    fetch_file_changes,
    file_table,
    new_line_range,
    numbered_file,
    patch_fingerprint,
    pr_diff_block,
    skip_reason,
    split_in_two,
    split_patch,
    walkthrough_diff,
)

# Errors that will hit every remaining call too: stop reviewing the file's other parts.
_RUN_WIDE_ERRORS = (llm.LLMUnavailable, llm.OutOfTime, llm.OverBudget)
_MAX_ANNOTATIONS = 10  # GitHub shows at most 10 error annotations per step


def main() -> None:
    # A console that can't encode a character (cp1252 on Windows, say) shouldn't crash the run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    print("Paul is on the case...")

    if not os.environ.get("PR_NUMBER"):
        event = os.environ.get("GITHUB_EVENT_NAME") or "unknown"
        print(f"::error::Paul reviews pull requests, but this run has no PR (event: {event}). "
              f"Run it on pull_request events.")
        sys.exit(1)

    try:
        config = load_config()
    except (ValueError, yaml.YAMLError) as e:
        print(f"::error::Paul's configuration is invalid: {e}")
        sys.exit(1)
    print(f"  Provider: {config['provider']} | Model: {config['model']} | Effort: {config['effort'] or 'default'} "
          f"| Config: {config['config_source']}")
    print(f"  Severity threshold: {config['severity_threshold']} | On incomplete: {config['on_incomplete']} "
          f"| Concurrency: {config['concurrency']}")

    pr = github_client.load_event().get("pull_request") or {}
    if not reviewer.api_key_available(config):
        _no_api_key(config, pr)
        return
    reviewer.set_api_key_env(config)

    llm.set_deadline(time.monotonic() + config["time_budget_minutes"] * 60)
    previous = github_client.find_summary_comment()
    ctx = _run_context(config, pr, previous)

    try:
        outcome = _review(config, ctx)
    except Exception as e:
        # A bug or an unexpected API failure: say so on the PR instead of leaving
        # the comment at "will update", and fail the check.
        traceback.print_exc()
        print(f"::error::Paul could not complete the review: {e}")
        try:
            github_client.upsert_summary_comment(render.format_failure(str(e), ctx), ctx["comment_id"])
        except Exception as post_error:
            print(f"::warning::Could not post the failure notice ({post_error}).")
        _write_outputs(ctx, verdict="error")
        sys.exit(1)

    _exit(outcome, ctx)


def _no_api_key(config: dict, pr: dict) -> None:
    """
    Fork and Dependabot PRs don't receive repository secrets, so a missing key is
    expected there. Anywhere else it means a misnamed or deleted secret, which
    must not turn the required check green.
    """
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or os.environ.get("REPO")
    author = (pr.get("user") or {}).get("login", "")
    if pr and (head_repo is None or head_repo != base_repo):
        reason = f"it comes from a fork ({head_repo or 'deleted repository'}), and forks don't receive repository secrets"
    elif author == "dependabot[bot]":
        reason = "Dependabot PRs don't receive repository secrets"
    else:
        print("::error::No LLM API key is available. Check that the action's api_key input points at an "
              f"existing secret with the key for the '{config['provider']}' provider.")
        sys.exit(1)

    if config["forks"] == "fail":
        print(f"::error::Paul can't review this PR: no LLM API key, because {reason}.")
        sys.exit(1)
    print(f"::notice::Paul skipped this PR: no LLM API key, because {reason}.")


def _run_context(config: dict, pr: dict, previous: dict | None) -> dict:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    prior_findings, prior_hashes = render.decode_state(previous["body"]) if previous else ([], {})
    return {
        "model": config["model"],
        "threshold": config["severity_threshold"],
        "config_source": config["config_source"],
        "head_sha": os.environ.get("HEAD_SHA", ""),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
        "pr_url": f"{server}/{repo}/pull/{os.environ.get('PR_NUMBER', '')}" if repo else "",
        "pr_title": pr.get("title") or "",
        "pr_body": pr.get("body") or "",
        "pr_changed_files": pr.get("changed_files") or 0,
        "prior_findings": prior_findings,
        "prior_file_hashes": prior_hashes,
        # Diff fingerprints stored with the findings. Until this run publishes its
        # own, comments keep the previous run's state as it was.
        "file_hashes": dict(prior_hashes),
        "comment_id": previous["id"] if previous else None,
        "coverage": Coverage(),
        "notes": [],
        "outcome": None,
        "result": None,
        "usage": llm.USAGE,
    }


def _review(config: dict, ctx: dict) -> str:
    coverage = ctx["coverage"]

    print("Fetching changed files...")
    changes = fetch_file_changes()
    if ctx["pr_changed_files"] > len(changes):
        coverage.fail_unlisted(ctx["pr_changed_files"] - len(changes), "over_file_limit")

    path_filter = PathFilter.from_config(config)
    reviewable = []
    for change in changes:
        reason = skip_reason(change, path_filter)
        if reason is None:
            reviewable.append(change)
        elif reason in FAIL_LABELS:
            coverage.fail(change.path, reason)
        else:
            coverage.skip(change.path, reason)
    max_files = config["budget"]["max_files"]
    for change in reviewable[max_files:]:
        coverage.fail(change.path, "budget")
    reviewable = reviewable[:max_files]
    print(f"  {len(changes)} changed file(s): {len(reviewable)} to review, "
          f"{len(coverage.skipped)} skipped, {coverage.failed_count} not reviewable")

    touched = [c.path for c in changes if is_trusted_path(c.path, config["config_path"])]
    if touched:
        names = ", ".join(render.code_span(p) for p in touched)
        ctx["notes"].append(
            f"This PR changes {names}. Paul reads its configuration and guidelines from the base "
            f"branch, so these changes apply to reviews after the PR merges."
        )

    result = {"summary": "", "changes": [], "issues": [], "test_recommendations": [], "resolved": [],
              "carried_findings": []}
    ctx["result"] = result
    current_hashes = {c.path: patch_fingerprint(c.patch) for c in reviewable}

    if reviewable:
        diff_block, compact = pr_diff_block(
            reviewable, config["review"]["pr_context_max_tokens"], config["review"]["diff_context"])
        if compact:
            print("  The PR's diff is too large to send with every call; using compact PR context.")
        plan = reviewer.build_plan(config, reviewer.pr_context(
            ctx["pr_title"], ctx["pr_body"], file_table(changes), diff_block))

        # ── Pass 1: Walkthrough (also writes the shared prefix to the cache) ──
        print("Pass 1: Generating walkthrough...")
        walkthrough = _walkthrough(reviewable, plan, config, compact)
        result.update(walkthrough)
        ctx["comment_id"] = github_client.upsert_summary_comment(
            render.format_walkthrough(walkthrough, ctx, len(reviewable)), ctx["comment_id"],
            fallback_body=render.format_walkthrough({"summary": "", "changes": []}, ctx, len(reviewable)),
        )
        print(f"  Comment posted (id={ctx['comment_id']}). Reviewers can see the walkthrough now.")

        # ── Pass 2: Issues per file, several at a time ───────────────────────
        print(f"Pass 2: Reviewing {len(reviewable)} file(s), {config['concurrency']} at a time...")
        with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
            futures = [
                pool.submit(_review_change, change, plan, ctx["prior_findings"], config, compact)
                for change in reviewable
            ]
            outcomes = [future.result() for future in futures]

        for change, (reviews, failure) in zip(reviewable, outcomes):
            if failure:
                coverage.fail(change.path, failure.reason)
            else:
                coverage.review(change.path)
            issues = [issue for review in reviews for issue in review["issues"]]
            result["issues"].extend(issues)
            for review in reviews:
                result["test_recommendations"].extend(review["test_recommendations"])
            if failure:
                continue
            prior = [f for f in ctx["prior_findings"] if f["file"] == change.path]
            unchanged = ctx["prior_file_hashes"].get(change.path) == current_hashes[change.path]
            resolved = _resolved(prior, reviews, issues, unchanged)
            result["resolved"].extend(resolved)
            if unchanged:
                # The code didn't change, so a finding the model didn't repeat isn't fixed:
                # keep it on record for the next run.
                repeated = {i["title"].strip().casefold() for i in issues}
                result["carried_findings"].extend(
                    f for f in prior if f["title"].strip().casefold() not in repeated and f not in resolved
                )
    elif coverage.failed_count:
        result["summary"] = "No file could be reviewed. See the list of files that were not reviewed."
    else:
        result["summary"] = "No reviewable changes: every changed file was skipped."

    # Findings on files that couldn't be reviewed this time stay on record for the next run,
    # with the fingerprint of the diff they were made against.
    failed_paths = coverage.failed_paths
    result["carried_findings"].extend(f for f in ctx["prior_findings"] if f["file"] in failed_paths)
    ctx["file_hashes"] = {path: current_hashes[path] for path in coverage.reviewed}
    ctx["file_hashes"].update({p: h for p, h in ctx["prior_file_hashes"].items() if p in failed_paths})

    overall = reviewer.highest_severity(result["issues"])
    result["overall_severity"] = overall
    outcome = reviewer.determines_outcome(
        overall, config["severity_threshold"], coverage.complete, config["on_incomplete"]
    )
    ctx["outcome"] = outcome
    usage = llm.USAGE
    cost = f" | Cost: ~${usage['cost_usd']:.2f}" if usage["calls"] and usage["cost_known"] else ""
    print(f"  Overall severity: {overall} | Issues found: {len(result['issues'])} | Outcome: {outcome}{cost}")
    _log_findings(result["issues"], config["severity_threshold"])

    # ── Publish ──────────────────────────────────────────────────────────────
    print("Updating comment with full review...")
    ctx["comment_id"] = github_client.upsert_summary_comment(
        render.format_comment(result, ctx), ctx["comment_id"], fallback_body=render.format_minimal(result, ctx)
    )
    if reviewable:
        github_client.submit_review(render.review_body(result, ctx))
    if outcome in ("pass", "neutral"):
        github_client.dismiss_stale_change_requests()
    _write_outputs(ctx, verdict=outcome)
    return outcome


def _walkthrough(reviewable: list, plan: llm.PromptPlan, config: dict, compact: bool) -> dict:
    if compact:
        diff_text, omitted = walkthrough_diff(reviewable)
        if omitted:
            print(f"  {len(omitted)} diff(s) left out of the walkthrough input to fit its budget.")
        task = reviewer.walkthrough_task(diff_text, omitted)
    else:
        task = reviewer.walkthrough_task()
    try:
        return reviewer.review_walkthrough(plan.with_task(task), config)
    except llm.ReviewError as e:
        # The walkthrough is informational; the per-file review still runs.
        print(f"::warning::Walkthrough unavailable ({e}); continuing with the per-file review.")
        return {"summary": f"*Walkthrough unavailable: {FAIL_LABELS.get(e.reason, e.reason)}.*", "changes": []}


def _review_change(change, plan: llm.PromptPlan, all_prior: list, config: dict, compact: bool) -> tuple:
    """
    Review one file, in parts if it is large, and in halves if a part is too much
    for one call. Runs on a worker thread. Every finished review is kept even when
    another part fails. Returns (reviews, the first failure or None).
    """
    llm.set_log_prefix(f"[{change.path}] ")
    print(f"  → {change.path}")
    left = llm.seconds_left()
    if left is not None and left < 15:
        return [], llm.OutOfTime("time_budget_minutes ran out")

    prior = [f for f in all_prior if f["file"] == change.path]
    file_text = numbered_file(change.path, _head_file(change.path), change.patch)
    parts = split_patch(change.patch)
    # (label, patch, may be split in two, is the whole file)
    work = [
        (change.path if len(parts) == 1 else f"{change.path} (part {i} of {len(parts)})", part, True, len(parts) == 1)
        for i, part in enumerate(parts, 1)
    ]
    reviews, failure = [], None
    while work:
        label, patch, can_split, whole = work.pop(0)
        # A partial view only hears about the earlier findings it can see, so it
        # can't declare one resolved without looking at the code.
        part_prior = prior if whole else _prior_in_range(prior, patch)
        # The whole-file diff is already in the cached PR context, except in compact mode.
        shown_patch = annotate_patch(patch) if compact or not whole else None
        try:
            task = reviewer.review_task(label, file_text, shown_patch, part_prior)
            reviews.append(reviewer.review_file(change.path, plan.with_task(task), config))
        except (llm.TruncatedOutput, llm.InputTooLarge) as e:
            halves = split_in_two(patch) if can_split else [patch]
            if len(halves) < 2:
                failure = failure or e
                llm.log(f"    Not reviewed ({label}): {e}")
                continue
            llm.log(f"    Too much for one call; reviewing {label} in two halves.")
            work[:0] = [(f"{label} (half {i} of 2)", half, False, False) for i, half in enumerate(halves, 1)]
        except llm.ReviewError as e:
            failure = failure or e
            llm.log(f"    Not reviewed ({label}): {e}")
            if isinstance(e, _RUN_WIDE_ERRORS):
                break
    return reviews, failure


def _head_file(path: str) -> str | None:
    """The file's text at the PR head (from the working directory outside a PR run), or None."""
    head_sha = os.environ.get("HEAD_SHA")
    if head_sha and os.environ.get("BASE_SHA"):
        try:
            return github_client.get_file_at(path, head_sha)
        except requests.RequestException as e:
            llm.log(f"    Couldn't fetch the file's current text ({e}); reviewing the diff alone.")
            return None
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    return None


def _prior_in_range(prior: list, patch: str) -> list:
    """The prior findings whose line falls inside this part of a split file."""
    first, last = new_line_range(patch)
    if first is None:
        return []
    return [f for f in prior if f["line_start"] and first <= f["line_start"] <= last]


def _resolved(prior: list, reviews: list, issues: list, unchanged: bool) -> list:
    """
    The prior findings the model says this diff fixes, matched by exact title.
    A finding reported again in this run is still open, whatever else the model said,
    and nothing is fixed in a file whose diff is the same as when it was reported.
    """
    if unchanged:
        return []
    claimed = {t.strip().casefold() for review in reviews for t in review["resolved_prior_findings"]}
    still_open = {issue["title"].strip().casefold() for issue in issues}
    return [f for f in prior if f["title"].strip().casefold() in claimed - still_open]


def _log_findings(issues: list, threshold: str) -> None:
    """
    Every finding goes to the run log, so nothing is lost when the comment has
    to be shortened. Blocking findings also become error annotations.
    """
    if not issues:
        return
    print("Findings:")
    blocking = reviewer.SEVERITY_ORDER.index(threshold)
    annotations = 0
    for issue in sorted(issues, key=lambda i: -reviewer.SEVERITY_ORDER.index(i["severity"])):
        where = f"{issue['file']}:{issue['line_start']}" if issue["line_start"] else issue["file"]
        print(f"  [{issue['severity']}] {_log_safe(where)}: {_log_safe(issue['title'])}")
        if reviewer.SEVERITY_ORDER.index(issue["severity"]) >= blocking and annotations < _MAX_ANNOTATIONS:
            annotations += 1
            line = f",line={issue['line_start']}" if issue["line_start"] else ""
            print(f"::error file={_command_property(issue['file'])}{line},title=Paul ({issue['severity']})::"
                  f"{_command_data(issue['title'])}")


def _write_outputs(ctx: dict, verdict: str) -> None:
    """Action outputs ($GITHUB_OUTPUT) and the job summary ($GITHUB_STEP_SUMMARY), when running in Actions."""
    coverage, result, usage = ctx["coverage"], ctx.get("result") or {}, llm.USAGE
    issues = result.get("issues", [])
    total_in = usage["input_tokens"]
    hit_ratio = usage["cache_read_tokens"] / total_in if total_in else 0.0
    cost = f"{usage['cost_usd']:.4f}" if usage["calls"] and usage["cost_known"] else ""
    comment_url = f"{ctx['pr_url']}#issuecomment-{ctx['comment_id']}" if ctx.get("pr_url") and ctx.get("comment_id") else ""
    outputs = {
        "verdict": verdict,
        "highest_severity": result.get("overall_severity", ""),
        "findings": str(len(issues)),
        "reviewed_files": str(len(coverage.reviewed)),
        "skipped_files": str(len(coverage.skipped)),
        "failed_files": str(coverage.failed_count),
        "cost_usd": cost,
        "cache_hit_ratio": f"{hit_ratio:.3f}",
        "comment_url": comment_url,
    }
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.writelines(f"{key}={value}\n" for key, value in outputs.items())
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(render.step_summary(ctx, outputs, hit_ratio))


def _log_safe(text: str) -> str:
    """One log line that can't start or smuggle a workflow command."""
    return re.sub(r"\s+", " ", str(text)).replace("::", ": :")


def _command_data(text: str) -> str:
    return _log_safe(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_property(text: str) -> str:
    return _command_data(text).replace(":", "%3A").replace(",", "%2C")


def _exit(outcome: str, ctx: dict) -> None:
    failed = ctx["coverage"].failed_count
    if outcome == "block":
        print(f"::error::Paul found issues at or above the '{ctx['threshold']}' threshold. See the review comment.")
        sys.exit(1)  # Non-zero exit makes the workflow job fail → blocks the PR
    if outcome == "fail":
        print(f"::error::Paul could not review {failed} file(s), so the check fails (on_incomplete: fail). "
              f"Re-run the job to retry.")
        sys.exit(1)
    if outcome == "neutral":
        print(f"::warning::Paul could not review {failed} file(s); passing because on_incomplete is neutral.")
    print("Paul found no blocking issues.")


if __name__ == "__main__":
    main()
