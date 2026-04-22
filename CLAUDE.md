# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Paul is a reusable GitHub Actions composite action (`action.yml`) that reviews pull requests using LLMs. It uses a **two-pass pipeline**: Pass 1 generates a walkthrough (summary + per-file change table) and posts it immediately as a PR comment; Pass 2 reviews each file's diff individually for issues and edits that comment with the full review. This architecture avoids JSON truncation on large PRs.

## Running Locally

```bash
pip install -r requirements.txt

# Required environment variables to run paul.py locally:
GITHUB_TOKEN=...   PAUL_API_KEY=...   PAUL_CONFIG_PATH=.paul.yml \
REPO=owner/repo    PR_NUMBER=42       HEAD_SHA=abc123 \
python src/paul.py
```

There are no tests. Manual integration testing is done by triggering the GitHub Action on an actual PR.

## Architecture

```
src/paul.py          — Entrypoint. Orchestrates the two-pass pipeline.
src/config.py        — Loads .paul.yml from the target repo workspace; falls back to DEFAULTS.
src/diff_processor.py — Fetches diff via GitHub API; filters excluded_paths globs; truncates to 80,000 chars (keeps largest files first).
src/reviewer.py      — LiteLLM calls with exponential backoff (30/60/120s). Pass 1: walkthrough_prompt.md. Pass 2: issues_prompt.md, one call per file.
src/github_client.py — GitHub API: post/edit PR comments, submit APPROVE/REQUEST_CHANGES review, set commit status.
prompts/system_prompt.md     — Legacy combined prompt (not used by current pipeline).
prompts/walkthrough_prompt.md — Pass 1 system prompt; expects JSON with overall_severity, summary, changes[].
prompts/issues_prompt.md      — Pass 2 system prompt; expects JSON with issues[], test_recommendations[].
```

## Key Design Decisions

- **LiteLLM** is the only LLM abstraction. Model strings are prefixed with the provider (e.g. `anthropic/claude-sonnet-4-6`) by `_resolve_model()` in `reviewer.py`.
- **`PAUL_API_KEY`** is the generic env var passed into the action; `_set_api_key_env()` maps it to the provider-specific var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) at runtime.
- **Blocking logic**: `determines_outcome()` in `reviewer.py` compares severity against threshold using `SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]`. A non-zero exit from `paul.py` is what actually blocks the PR merge via GitHub Actions job status.
- **Walkthrough comment is posted before Pass 2 starts** so reviewers see something immediately; the comment is then edited in-place via `edit_comment()` once all files are reviewed.
- **`split_into_file_blocks`** is defined privately in `diff_processor.py` and re-exported as a public alias; `reviewer.py` imports it directly.

## Automatic Codebase Context

At startup, `load_config()` calls `read_repo_context()` (in `config.py`) which reads the following files from the workspace root and injects them into both Pass 1 and Pass 2 system prompts via the `{REPO_CONTEXT}` placeholder:

- `CLAUDE.md`
- `README.md`
- `.agents/rules/*.md` (all files, sorted)

Each file is capped at 8,000 characters. Files that don't exist are silently skipped. The injected block appears before `{CUSTOM_INSTRUCTIONS}` under a **"Codebase Context"** heading, so Paul understands existing patterns before flagging issues.

## Configuration (`.paul.yml`)

Consuming repos place `.paul.yml` in their root. See `.paul.yml.example` for all options. The `custom_instructions` field is injected into the system prompt at the `{CUSTOM_INSTRUCTIONS}` placeholder in the prompt templates.

Valid `provider` values: `anthropic`, `openai`, `google`.  
Valid `severity_threshold` values: `critical`, `major`, `minor`.  
Default model: `claude-sonnet-4-6`.
