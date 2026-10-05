# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Paul is a reusable GitHub Actions composite action (`action.yml`) that reviews pull requests using LLMs. Pass 1 generates a walkthrough (summary + per-file change table) and posts it immediately as a PR comment; Pass 2 reviews each changed file in its own call, `concurrency` files at a time, and edits that comment with the full review. The job's exit code is the merge gate, and the gate **fails closed**: a file with changes that couldn't be reviewed never counts as clean.

## Running Locally

```bash
pip install -r requirements-litellm.lock   # hash-checked; the core lock plus LiteLLM
pip install -r requirements-dev.txt
python -m pytest                           # no network or API key needed

# A real run against a PR (BASE_SHA is optional: without it, .paul.yml, the guideline
# files and the files under review are read from the working directory):
GITHUB_TOKEN=...   PAUL_API_KEY=...   PAUL_CONFIG_PATH=.paul.yml \
REPO=owner/repo    PR_NUMBER=42       HEAD_SHA=abc123   BASE_SHA=def456 \
python src/paul.py
```

`tests/conftest.py` fakes the GitHub API (`FakeGitHub`) and the Claude transport, `llm_anthropic.send` (`FakeLLM`). `tests/test_contract.py` runs the real SDKs against a local HTTP server to pin down the wire format, streaming included; run it after any SDK upgrade. CI also checks that the Claude path never imports LiteLLM. `self-review.yml` has Paul review its own PRs with the branch's code (`uses: ./`).

## Architecture

```
src/paul.py           — Entrypoint: preflight (no API key: skip fork and Dependabot PRs, fail any
                        other), Pass 1, Pass 2 on a thread pool, the coverage ledger, the gate, step
                        outputs and job summary, and a catch-all that posts a failure notice.
src/config.py         — Loads .paul.yml and the guideline files from the PR's base commit; DEFAULTS
                        with three sections (cache, review, budget); validates every setting.
src/provider.py       — Writes the configured provider to GITHUB_OUTPUT so action.yml installs
                        LiteLLM only for OpenAI and Gemini.
src/diff_processor.py — Changed files, path filters, the PR <diff> block (full or compact),
                        line-numbered patches and file text, patch splitting, diff fingerprints.
src/reviewer.py       — Builds the prompt plan and tasks; parses and normalizes the JSON answers
                        (one re-ask on unusable output); severity normalization; the gate.
src/llm.py            — Provider-neutral calls: PromptPlan, retries with backoff and retry-after,
                        the time and cost budgets, token and cost totals, the ReviewError types.
src/llm_anthropic.py  — Claude on the Anthropic SDK: cache breakpoints, structured outputs, effort,
                        refusal fallbacks, model capabilities from the Models API.
src/llm_litellm.py    — OpenAI and Gemini through LiteLLM (imported only for those providers).
src/schemas.py        — ENVELOPE, the one JSON schema every call answers in.
src/render.py         — PR comment, review body and job summary; escaping, the 60,000-character
                        budget, hidden markers (trusted only at the end of a bot-authored comment).
src/coverage.py       — The ledger: every changed file is reviewed, skipped by design, or failed.
src/github_client.py  — GitHub API with retries and pagination.
prompts/static_rules.md     — Paul's rules and output format: cache layer 1, the same for every call.
prompts/task_walkthrough.md — The Pass 1 task.
prompts/task_review.md      — The Pass 2 task; {label} names the file (or part) to review.
```

## Key Design Decisions

- **One shared prompt prefix per run.** Every call is a `llm.PromptPlan`: static rules → repo context (guideline files, `custom_instructions`, `language`) → PR context (title, description, file table, `<diff>`) → task. On Claude these are three `cache_control` breakpoints (`cache.ttl` for the first two, 5m for the PR); the walkthrough call writes the cache and every file's review reads it. Per-call data (the file's text, its prior findings, a part of a split patch) goes in the task only. One model, effort and output schema per run: changing any of them breaks the cache.
- **One output schema.** Every task answers in `schemas.ENVELOPE` (`task` plus one filled section). Models without structured outputs (such as claude-sonnet-4-6) may answer with the bare section; `reviewer._section()` accepts both.
- **Transports.** `llm.complete()` owns retries, budgets and usage; a transport (`llm_anthropic`, `llm_litellm`) maps a plan to a request and provider errors to `llm.Retry`, a `ReviewError` (this file fails) or `llm.Fatal` (bad key, unknown model, billing: the run ends). Claude calls always stream; errors are classified by `e.type`, since a stream that fails part-way reports status 200. SDK 1.x: temperature goes in `extra_body`; `ModelCapabilities` is read with `.to_dict()`; Claude 5.x calls use the beta endpoint with `fallbacks="default"`. LiteLLM is imported lazily.
- **`PAUL_API_KEY`** is mapped to the provider's variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) by `reviewer.set_api_key_env()`.
- **Blocking logic**: `determines_outcome()` in `reviewer.py` returns `block` (a finding at or above `severity_threshold`, using `SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]`), then `fail`/`neutral` (a file went unreviewed, per `on_incomplete`; neutral covers only `coverage.NEUTRAL_REASONS`, provider outages), else `pass`. Unrecognized severity labels count as `critical`. A non-zero exit from `paul.py` is what blocks the merge.
- **Per-file failures** raise a `ReviewError` with a `reason` from `coverage.FAIL_LABELS`; `paul.py` records the file as failed. An oversized call is retried without the file's text, then in halves; a cut-off answer in halves. `LLMUnavailable`, `OutOfTime` and `OverBudget` stop the file's other parts; any other unexpected error fails that file only.
- **Prior findings and resolutions.** The hidden state block stores `{"findings": [...], "files": {path: diff fingerprint}}`. A prior finding counts as resolved only if the model claims it, doesn't report it again, and the file's diff fingerprint changed; in an unchanged file it is carried forward.
- **Trust boundary.** Config and guideline files come from the base commit, and file text from the contents API at `HEAD_SHA`, so the PR's code never runs on the runner. Diffs, PR text, paths and model output are untrusted: `diff_processor.neutralize`/`one_line` defuse Paul's prompt tags in them, and they are escaped in comments and log lines.
- **Paul never approves.** It submits `COMMENT` reviews, and dismisses older versions' `REQUEST_CHANGES` once a run passes.
- **Supply chain.** `requirements.lock` (always installed) and `requirements-litellm.lock` (on top, for OpenAI and Gemini) are hash-locked; regenerate both with the commands in `requirements.in`. Actions are pinned to commit SHAs.

## Automatic Codebase Context

`load_config()` calls `read_repo_context()` (in `config.py`), which reads `CLAUDE.md`, `README.md` and `.agents/rules/*.md` (sorted) from the PR's base commit, each capped at 8,000 characters; missing files are skipped. They form the repo-context layer under a **"Codebase Context"** heading, before `custom_instructions` and the `language` directive.

## Configuration (`.paul.yml`)

Consuming repos place `.paul.yml` in their root; `.paul.yml.example` documents every option. Unknown keys produce a warning; invalid values fail the run. `provider`: `anthropic`, `openai`, `google`. `severity_threshold`: `critical`, `major`, `minor`. `on_incomplete`: `fail` (default), `neutral`. `forks`: `skip` (default), `fail`. Defaults: model `claude-sonnet-5-5`, effort `medium`, concurrency 4, `cache.ttl` 5m, `budget.max_cost_usd` 5.0.
