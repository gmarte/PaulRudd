"""
Paul — AI PR Review Bot entry point.

  Preflight → no API key: skip fork and Dependabot PRs with a notice, fail any other
  Pass 1    → walkthrough (summary + changes) → summary comment posted immediately;
              this first call also writes the shared prompt prefix to the cache
  Pass 2    → issues per file, `concurrency` files at a time, each reading the cache
  Gate      → exit 1 on a finding at or above the threshold, or when a file with
              changes couldn't be reviewed (on_incomplete: neutral excuses only
              files the LLM provider couldn't answer for)

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
_MAX_ANNOTATIONS = 10        # GitHub shows at most 10 error annotations per step
_HEAD_FILE_MIN_SECONDS = 60  # fetch a file's current text only with this much of the time budget left
_HEAD_FILE_MAX_WAIT = 30     # longest GitHub rate-limit wait for it; the review goes on without it


class _InternalError(llm.ReviewError):
    """A bug, or an error no transport maps: the file fails, the others go on."""
    reason = "internal_error"


def main() -> None:
    # A console that can't encode a character (cp1252 on Windows, say) shouldn't crash the run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        # A failure before the review started (reading the PR's comments, say).
        _set_outputs(verdict="error")
        raise


def _main() -> None:
    print("Paul is on the case...")

    if not os.environ.get("PR_NUMBER"):
        event = os.environ.get("GITHUB_EVENT_NAME") or "unknown"
        print(f"::error::Paul reviews pull requests, but this run has no PR (event: {_command_data(event)}). "
              f"Run it on pull_request events.")
        _set_outputs(verdict="error")
        sys.exit(1)

    try:
        config = load_config()
    except (ValueError, yaml.YAMLError) as e:
        print(f"::error::Paul's configuration is invalid: {_command_data(str(e))}")
        _set_outputs(verdict="error")
        sys.exit(1)
    print(f"  Provider: {config['provider']} | Model: {config['model']} | Effort: {config['effort'] or 'default'} "
          f"| Config: {config['config_source']}")
    print(f"  Severity threshold: {config['severity_threshold']} "
          f"| Min confidence to block: {config['min_confidence_to_block']} "
          f"| On incomplete: {config['on_incomplete']} | Concurrency: {config['concurrency']}")

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
        # A bug, a fatal provider error (a bad key, say) or a GitHub failure: say so on
        # the PR instead of leaving the comment at "will update", and fail the check.
        _print_traceback()
        print(f"::error::Paul could not complete the review: {_command_data(str(e))}")
        try:
            ctx["comment_id"] = github_client.upsert_summary_comment(render.format_failure(str(e), ctx),
                                                                     ctx["comment_id"])
        except Exception as post_error:
            print(f"::warning::Could not post the failure notice ({_command_data(str(post_error))}).")
        _write_outputs(ctx, verdict="error")
        sys.exit(1)

    _write_outputs(ctx, verdict=outcome)
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
        _set_outputs(verdict="error")
        sys.exit(1)

    if config["forks"] == "fail":
        print(f"::error::Paul can't review this PR: no LLM API key, because {_command_data(reason)}.")
        _set_outputs(verdict="fail")
        sys.exit(1)
    print(f"::notice::Paul skipped this PR: no LLM API key, because {_command_data(reason)}.")
    _set_outputs(verdict="skipped")


def _run_context(config: dict, pr: dict, previous: dict | None) -> dict:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    prior_findings, prior_hashes = render.decode_state(previous["body"]) if previous else ([], {})
    return {
        "model": config["model"],
        "threshold": config["severity_threshold"],
        "min_confidence": config["min_confidence_to_block"],
        "on_incomplete": config["on_incomplete"],
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
        review = config["review"]
        diff_block, compact = pr_diff_block(reviewable, review["pr_context_max_tokens"], review["diff_context"])
        if compact:
            print("  The PR's diff is too large to send with every call; using compact PR context.")
        plan = _plan(config, ctx, changes, diff_block)

        # ── Pass 1: Walkthrough (also writes the shared prefix to the cache) ──
        print("Pass 1: Generating walkthrough...")
        walkthrough, error = _walkthrough(reviewable, plan, config, compact)
        if isinstance(error, llm.InputTooLarge) and not compact:
            # The size estimate was off (dense text such as CJK, base64 or minified code).
            print("  The PR's diff doesn't fit the model's context window; using compact PR context.")
            diff_block, compact = pr_diff_block(reviewable, review["pr_context_max_tokens"], "compact")
            plan = _plan(config, ctx, changes, diff_block)
            walkthrough, error = _walkthrough(reviewable, plan, config, compact)
        if error:
            # The walkthrough is informational; the per-file review still runs.
            print(f"::warning::Walkthrough unavailable ({_command_data(str(error))}); "
                  f"continuing with the per-file review.")
            walkthrough = {"summary": f"*Walkthrough unavailable: {FAIL_LABELS.get(error.reason, error.reason)}.*",
                           "changes": []}
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
            try:
                outcomes = [future.result() for future in futures]
            except llm.Fatal:
                for future in futures:
                    future.cancel()  # the files not started yet would fail the same way
                raise

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

    _record_failed_files(ctx, result, current_hashes)

    overall = reviewer.highest_severity(result["issues"])
    result["overall_severity"] = overall
    # Findings less sure than min_confidence_to_block are shown but don't block.
    gating = reviewer.gating_severity(result["issues"], config["min_confidence_to_block"])
    # on_incomplete: neutral excuses only files the provider couldn't answer for; files
    # left unreviewed for any other reason (size, budgets, refusals, unusable output)
    # fail the check, since a PR's author could cause those on purpose.
    on_incomplete = config["on_incomplete"] if coverage.excusable else "fail"
    outcome = reviewer.determines_outcome(gating, config["severity_threshold"], coverage.complete, on_incomplete)
    ctx["outcome"] = outcome
    usage = llm.USAGE
    cost = f" | Cost: ~${usage['cost_usd']:.2f}" if usage["calls"] and usage["cost_known"] else ""
    print(f"  Overall severity: {overall} | Issues found: {len(result['issues'])} | Outcome: {outcome}{cost}")
    _log_findings(result["issues"], config["severity_threshold"], config["min_confidence_to_block"])

    # ── Publish ──────────────────────────────────────────────────────────────
    print("Updating comment with full review...")
    ctx["comment_id"] = github_client.upsert_summary_comment(
        render.format_comment(result, ctx), ctx["comment_id"], fallback_body=render.format_minimal(result, ctx)
    )
    if reviewable:
        github_client.submit_review(render.review_body(result, ctx))
    if outcome in ("pass", "neutral"):
        github_client.dismiss_stale_change_requests()
    return outcome


def _plan(config: dict, ctx: dict, changes: list, diff_block: str) -> llm.PromptPlan:
    return reviewer.build_plan(config, reviewer.pr_context(ctx["pr_title"], ctx["pr_body"], file_table(changes),
                                                           diff_block))


def _record_failed_files(ctx: dict, result: dict, current_hashes: dict) -> None:
    """
    What the next run should know about the files that weren't fully reviewed: their
    earlier findings stay on record (unless reported again), next to the fingerprint
    of the diff the stored findings were made against.
    """
    failed_paths = ctx["coverage"].failed_paths
    new_titles = {}
    for issue in result["issues"]:
        new_titles.setdefault(issue["file"], set()).add(issue["title"].strip().casefold())
    result["carried_findings"].extend(
        f for f in ctx["prior_findings"]
        if f["file"] in failed_paths and f["title"].strip().casefold() not in new_titles.get(f["file"], set())
    )
    hashes = {path: current_hashes[path] for path in ctx["coverage"].reviewed}
    for path in failed_paths:
        if path in new_titles and path in current_hashes:
            hashes[path] = current_hashes[path]  # a part of it was reviewed against this diff
        elif path in ctx["prior_file_hashes"]:
            hashes[path] = ctx["prior_file_hashes"][path]
    ctx["file_hashes"] = hashes


def _walkthrough(reviewable: list, plan: llm.PromptPlan, config: dict, compact: bool) -> tuple:
    """Pass 1. Returns (walkthrough, None), or (None, the ReviewError) when it failed."""
    if compact:
        diff_text, omitted = walkthrough_diff(reviewable)
        if omitted:
            print(f"  {len(omitted)} diff(s) left out of the walkthrough input to fit its budget.")
        task = reviewer.walkthrough_task(diff_text, omitted)
    else:
        task = reviewer.walkthrough_task()
    try:
        return reviewer.review_walkthrough(plan.with_task(task), config), None
    except llm.ReviewError as e:
        return None, e


def _review_change(change, plan: llm.PromptPlan, all_prior: list, config: dict, compact: bool) -> tuple:
    """
    Review one file on a worker thread. Returns (reviews, the first failure or None).
    A fatal error (a bad key, say) ends the run; any other unexpected error fails
    this file only.
    """
    llm.set_log_prefix(f"[{_log_safe(change.path)}] ")
    print(f"  → {_log_safe(change.path)}")
    try:
        return _review_parts(change, plan, all_prior, config, compact)
    except llm.Fatal:
        raise
    except Exception as e:
        return [], _internal_error(e)


def _review_parts(change, plan: llm.PromptPlan, all_prior: list, config: dict, compact: bool) -> tuple:
    """
    Review a file whole, or in parts if it is large. A call that is too large is
    retried without the file's full text, then (when that makes it smaller) in
    halves; a cut-off answer is retried in halves. Every finished review is kept
    even when another part fails.
    """
    left = llm.seconds_left()
    if left is not None and left < 15:
        return [], llm.OutOfTime("time_budget_minutes ran out")

    prior = [f for f in all_prior if f["file"] == change.path]
    file_text = numbered_file(change.path, _head_file(change.path), change.patch)
    parts = split_patch(change.patch)
    # (label, patch, may be split in two, is the whole file, send the file's text)
    work = [
        (change.path if len(parts) == 1 else f"{change.path} (part {i} of {len(parts)})", part, True, len(parts) == 1,
         True)
        for i, part in enumerate(parts, 1)
    ]
    reviews, failure = [], None
    while work:
        label, patch, can_split, whole, with_file = work.pop(0)
        # A partial view only hears about the earlier findings it can see, so it
        # can't declare one resolved without looking at the code.
        part_prior = prior if whole else _prior_in_range(prior, patch)
        # The whole-file diff is already in the cached PR context, except in compact mode.
        shown_patch = annotate_patch(patch) if compact or not whole else None
        try:
            task = reviewer.review_task(label, file_text if with_file else None, shown_patch, part_prior)
            reviews.append(reviewer.review_file(change.path, plan.with_task(task), config))
            continue
        except llm.InputTooLarge as e:
            if with_file and file_text:
                llm.log(f"    Too large for one call; reviewing {_log_safe(label)} without the file's full text.")
                work.insert(0, (label, patch, can_split, whole, False))
                continue
            # Halves help only when the task carries the diff; otherwise the shared context is what's too large.
            halves = split_in_two(patch) if can_split and shown_patch is not None else [patch]
            error, keep_file = e, False
        except llm.TruncatedOutput as e:
            halves = split_in_two(patch) if can_split else [patch]
            error, keep_file = e, with_file
        except llm.ReviewError as e:
            failure = failure or e
            llm.log(f"    Not reviewed ({_log_safe(label)}): {e}")
            if isinstance(e, _RUN_WIDE_ERRORS):
                break
            continue
        except llm.Fatal:
            raise
        except Exception as e:
            error = _internal_error(e)  # logged even when an earlier part already failed
            failure = failure or error
            continue
        if len(halves) < 2:
            failure = failure or error
            llm.log(f"    Not reviewed ({_log_safe(label)}): {error}")
            continue
        llm.log(f"    Too much for one call; reviewing {_log_safe(label)} in two halves.")
        work[:0] = [(f"{label} (half {i} of 2)", half, False, False, keep_file) for i, half in enumerate(halves, 1)]
    return reviews, failure


def _internal_error(error: Exception) -> llm.ReviewError:
    """Log an unexpected error with its traceback (call it from the except block) and wrap it."""
    llm.log(f"    Not reviewed: unexpected {type(error).__name__}. Traceback:")
    for line in traceback.format_exc().splitlines():
        llm.log(f"    {_log_safe(line)}")
    return _InternalError(f"{type(error).__name__}: {error}")


def _head_file(path: str) -> str | None:
    """The file's text at the PR head (from the working directory outside a PR run), or None."""
    head_sha = os.environ.get("HEAD_SHA")
    if head_sha and os.environ.get("BASE_SHA"):
        left = llm.seconds_left()
        if left is not None and left < _HEAD_FILE_MIN_SECONDS:
            return None
        try:
            return github_client.get_file_at(path, head_sha, max_wait=_HEAD_FILE_MAX_WAIT)
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


def _log_findings(issues: list, threshold: str, min_confidence: str) -> None:
    """
    Every finding goes to the run log, so nothing is lost when the comment has
    to be shortened. Blocking findings also become error annotations.
    """
    if not issues:
        return
    print("Findings:")
    annotations = 0
    for issue in sorted(issues, key=lambda i: -reviewer.SEVERITY_ORDER.index(i["severity"])):
        where = f"{issue['file']}:{issue['line_start']}" if issue["line_start"] else issue["file"]
        print(f"  [{issue['severity']}] {_log_safe(where)}: {_log_safe(issue['title'])}")
        if reviewer.blocks(issue, threshold, min_confidence) and annotations < _MAX_ANNOTATIONS:
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
    _set_outputs(**outputs)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(render.step_summary(ctx, outputs, hit_ratio))
        except OSError as e:
            print(f"::warning::Could not write the job summary ({_command_data(str(e))}).")


def _set_outputs(**values) -> None:
    """Append step outputs; a failure to write them never changes the verdict."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(f"{key}={value}\n" for key, value in values.items())
    except OSError as e:
        print(f"::warning::Could not write the step outputs ({_command_data(str(e))}).")


def _print_traceback() -> None:
    """The current exception's traceback, with nothing in it able to start a workflow command."""
    for line in traceback.format_exc().splitlines():
        print(line.replace("::", ": :"))


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
        if ctx["on_incomplete"] == "neutral":
            print(f"::error::Paul could not review {failed} file(s), so the check fails: on_incomplete: neutral "
                  f"only excuses files the LLM provider couldn't answer for. See the review comment for the reasons.")
        else:
            print(f"::error::Paul could not review {failed} file(s), so the check fails (on_incomplete: fail). "
                  f"Re-run the job to retry.")
        sys.exit(1)
    if outcome == "neutral":
        print(f"::warning::Paul could not review {failed} file(s); passing because on_incomplete is neutral.")
    print("Paul found no blocking issues.")


if __name__ == "__main__":
    main()
