# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Paul is a reusable GitHub Actions composite action (`action.yml`) that reviews pull requests using LLMs. It uses a **two-pass pipeline**: Pass 1 generates a walkthrough (summary + per-file change table) and posts it immediately as a PR comment; Pass 2 reviews each file's diff individually for issues and edits that comment with the full review. The job's exit code is the merge gate, and the gate **fails closed**: a file with changes that couldn't be reviewed never counts as clean.

## Running Locally

```bash
pip install -r requirements.lock      # hash-checked
pip install -r requirements-dev.txt
python -m pytest                      # the test suite; no network or API key needed

# A real run against a PR (BASE_SHA is optional: without it, .paul.yml and the
# guideline files are read from the working directory instead of the base commit):
GITHUB_TOKEN=...   PAUL_API_KEY=...   PAUL_CONFIG_PATH=.paul.yml \
REPO=owner/repo    PR_NUMBER=42       HEAD_SHA=abc123   BASE_SHA=def456 \
python src/paul.py
```

`tests/conftest.py` provides an in-memory GitHub API (`FakeGitHub`) and a scripted stand-in for `litellm.completion` (`FakeLLM`). `tests/test_contract.py` runs the real LiteLLM call path against a local HTTP server to pin down the request bodies sent to each provider. CI (`.github/workflows/ci.yml`) runs the suite; `self-review.yml` has Paul review its own PRs with the branch's code (`uses: ./`).

## Architecture

```
src/paul.py           — Entrypoint. Preflight (no API key: skip fork and Dependabot PRs, fail any
                        other), Pass 1, Pass 2 within time_budget_minutes, the gate, and a catch-all
                        that turns a crash into a failure notice on the PR.
src/config.py         — Loads .paul.yml and the guideline files from the PR's base commit (via the
                        GitHub contents API); falls back to DEFAULTS. Validates every setting.
src/diff_processor.py — Fetches changed files from the paginated PR files API; gitignore-style path
                        filters (pathspec) on top of the defaults in config.py (file-name defaults match
                        only a file's own name); line-numbered patches; splits oversized patches at hunk
                        boundaries; budgets the Pass 1 diff.
src/coverage.py       — The ledger: every changed file is reviewed, skipped by design, or failed.
src/reviewer.py       — LiteLLM calls with retries (429, 5xx/529, timeouts; honours retry-after).
                        Tolerant JSON parsing with one re-ask, output normalization, prompt assembly.
src/render.py         — Renders the summary comment: escaping, the 60,000-character budget, hidden
                        markers (<!-- paul:summary -->, <!-- paul:findings ... -->). The markers are
                        only trusted at the very end of a bot-authored comment, so LLM text can't
                        plant review history.
src/github_client.py  — GitHub API: one request() with retries and GITHUB_API_URL support, pagination,
                        find/update Paul's summary comment, COMMENT reviews, dismissing stale reviews.
prompts/walkthrough_prompt.md — Pass 1 system prompt; expects JSON with summary, changes[].
prompts/issues_prompt.md      — Pass 2 system prompt; expects JSON with issues[], test_recommendations[],
                                resolved_prior_findings[].
```

## Key Design Decisions

- **LiteLLM** is the only LLM abstraction. Model strings are prefixed with the provider (e.g. `anthropic/claude-sonnet-4-6`) by `resolve_model()` in `reviewer.py`. LiteLLM is pinned exactly in `requirements.in`; the action installs `requirements.lock` with `--require-hashes`.
- **`PAUL_API_KEY`** is the generic env var passed into the action; `_set_api_key_env()` maps it to the provider-specific var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) at runtime.
- **The system prompt is a system message.** On Anthropic it is sent as a content block with `cache_control`, so every file after the first reads it from the prompt cache. Keep it identical across the files of a run: per-file data (PR text, prior findings, the diff) goes in the user message.
- **Temperature is opt-in** and is never sent to models matched by `_NO_SAMPLING_MODELS` (Claude Opus 4.7+, Sonnet 5+), which reject it.
- **Blocking logic**: `determines_outcome()` in `reviewer.py` returns `block` (a finding at or above `severity_threshold`, using `SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]`), then `fail`/`neutral` (a file went unreviewed, per `on_incomplete`), else `pass`. Severity labels are matched loosely (case, accents, decoration, translations such as "crítico" or "majeur"), and anything unrecognized counts as `critical`, so an odd label can't slip under any threshold. A non-zero exit from `paul.py` is what actually blocks the PR merge.
- **Per-file LLM failures raise a `ReviewError`** with a `reason` from `coverage.FAIL_LABELS`; `paul.py` records the file as failed, and a truncated response is retried once in two halves. Configuration errors (bad API key, unknown model, missing permission) are not `ReviewError`s: they abort the run, which posts a failure notice and exits 1.
- **Paul never approves.** It submits `COMMENT` reviews and dismisses `REQUEST_CHANGES` reviews left by older versions once a run passes.
- **Walkthrough comment is posted before Pass 2 starts** so reviewers see something immediately; the same comment is then edited in place, on this push and on later ones. Only bot-authored comments carrying the summary marker count as Paul's.

## Automatic Codebase Context

At startup, `load_config()` calls `read_repo_context()` (in `config.py`) which reads the following files from the PR's base commit and injects them into both Pass 1 and Pass 2 system prompts via the `{REPO_CONTEXT}` placeholder:

- `CLAUDE.md`
- `README.md`
- `.agents/rules/*.md` (all files, sorted)

Each file is capped at 8,000 characters. Files that don't exist are silently skipped. The injected block appears before `{CUSTOM_INSTRUCTIONS}` under a **"Codebase Context"** heading, so Paul understands existing patterns before flagging issues. Placeholders are substituted in a single pass, so placeholder text inside these files is left as is.

## Configuration (`.paul.yml`)

Consuming repos place `.paul.yml` in their root. See `.paul.yml.example` for all options. The `custom_instructions` field is injected into the system prompt at the `{CUSTOM_INSTRUCTIONS}` placeholder in the prompt templates, followed by the `language` directive when set. Unknown keys produce a warning.

Valid `provider` values: `anthropic`, `openai`, `google`.
Valid `severity_threshold` values: `critical`, `major`, `minor`.
Valid `on_incomplete` values: `fail` (default), `neutral`. Valid `forks` values: `skip` (default), `fail`.
Default model: `claude-sonnet-4-6`.
