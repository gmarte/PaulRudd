# Paul 👨‍⚖️

> **Paul Rudd = PR.** A reusable GitHub Action that reviews pull requests using LLMs.

Paul is a self-hosted AI code reviewer you deploy once and reuse across all your repositories. Drop a workflow file into any repo, add an API key secret, and every PR gets an automated review with a severity-gated status check.

---

## Features

- **4-tier severity model** — `critical`, `major`, `minor`, `suggestion` with a configurable blocking threshold
- **A gate that fails closed** — every changed file is reviewed or listed with the reason it wasn't; a file that couldn't be reviewed never counts as clean
- **Whole-PR context** — each file is reviewed with the rest of the PR in view (its description, the changed-file list and every diff) plus the file's current text, so a change that breaks a caller in another file can be caught
- **Prompt caching by design** — every call in a run shares one prefix (Paul's rules → your repo's guidelines → the PR), so after the first call each file's review reads most of its input from the cache at a tenth of the price. See [Prompt Caching](#prompt-caching)
- **Fast on big PRs** — files are reviewed 4 at a time (`concurrency`), within a time budget and a cost cap
- **Structured outputs** — on models that support them, every answer follows one JSON schema; other models get tolerant parsing and a second try
- **Multi-LLM support** — Claude through the official Anthropic SDK; OpenAI GPT and Google Gemini through [LiteLLM](https://github.com/BerriAI/litellm), installed only when you choose them. Swap providers by changing two lines in `.paul.yml`
- **Per-repo configuration** — `.paul.yml` controls the model, severity threshold, excluded paths, language and custom review instructions. It is read from the base branch, so a PR can't change the rules it is reviewed by (see [Hardening the gate](#hardening-the-gate) to protect the workflow file too)
- **Outputs for your workflow** — the verdict, findings, estimated cost and cache hit ratio as step outputs, plus a job summary
- **AI-agent-ready output** — a ready-to-paste prompt covering every finding, with `autofix` replacements where possible
- **Locked-down supply chain** — hash-checked dependencies and actions pinned to commit SHAs

---

## How It Works

```
PR opened / updated
        │
        ▼
  Preflight: fork or Dependabot PR without an API key? → skip with a notice
             (a missing key on any other PR fails the check)
        │
        ▼
  Read .paul.yml + CLAUDE.md, README.md, .agents/rules/ from the base branch
        │
        ▼
  Fetch changed files (paginated) → skip excluded, deleted, binary files
        │
        ▼
  Build the shared prompt prefix: rules → repo guidelines → PR description + diff
        │
        ▼
  Pass 1: walkthrough (summary + changes) → summary comment posted right away
          (this first call also writes the prefix to the prompt cache)
        │
        ▼
  Pass 2: one call per file (large files in parts), 4 at a time, each reading
          the prefix from the cache and adding the file's current text → findings
        │
        ▼
  Update the summary comment · submit a COMMENT review · outputs + job summary
        │
        ▼
  Exit 1 if a finding meets the threshold, or a file couldn't be reviewed
```

The job's exit code is the gate: make **Paul / AI PR Review** a required check. Paul never approves PRs.

---

## Quick Start

### 1. Add Paul to a repo

Copy [`examples/paul-workflow.yml`](examples/paul-workflow.yml) to `.github/workflows/paul.yml` in the target repository:

```yaml
name: Paul PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:   # only if you use a merge queue

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: paul-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  paul:
    name: "Paul / AI PR Review"
    if: ${{ github.event_name == 'pull_request' && !github.event.pull_request.draft }}
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: gmarte/PaulRudd@v2
        with:
          api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

No checkout step is needed: Paul reads the diff and the config through the GitHub API. Paul runs on Linux, macOS and Windows runners.

### 2. Add your API key secret

In the target repo: **Settings → Secrets and variables → Actions → New repository secret**

| Provider | Secret name |
|----------|------------|
| Anthropic (default) | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GOOGLE_API_KEY` |

### 3. (Optional) Add a `.paul.yml` config

Copy `.paul.yml.example` to `.paul.yml` in the repo root. Paul reads it from the PR's base branch, so a change to it applies once it is merged.

```yaml
provider: anthropic
model: claude-sonnet-5-5
severity_threshold: major   # critical | major | minor

excluded_paths:             # added to the built-in defaults (lockfiles, dist/, build/, …)
  - "**/migrations/**"
  - "docs/**"

custom_instructions: |
  This is a Python/FastAPI REST API.
  Pay special attention to:
  - SQL injection via raw queries
  - Missing authentication on new endpoints
  - Unhandled exceptions on external service calls
```

### 4. Enable branch protection (recommended)

**Settings → Branches → Branch protection rules** for `main`/`master`:
- Enable **Require status checks to pass before merging**
- Add **Paul / AI PR Review** as a required check

### Hardening the gate

Paul reads `.paul.yml` and the guideline files from the base branch, so a PR can't loosen its own review settings. The workflow file is different: on `pull_request` events, a PR runs its own copy of `.github/workflows/paul.yml`, so it could edit the job away and still report a green check. To close that gap:

- Add a `CODEOWNERS` entry for `.github/workflows/`, `.paul.yml`, `CLAUDE.md`, `README.md` and `.agents/rules/`, and enable **Require review from Code Owners**. Or use an organization ruleset that requires the workflow from the default branch.
- Pass `GITHUB_TOKEN` (the default) or a GitHub App token as `github_token`. Paul recognizes its own comments by their bot author; with a personal access token it can't find its earlier review, so it posts a new comment on every push and loses track of earlier findings.

---

## Severity Model

| Severity | Check result (threshold `major`) | Examples |
|----------|-----------|---------|
| 🔴 **critical** | Fails | Security vulns, exposed secrets, SQL injection, auth bypass, data loss |
| 🟠 **major** | Fails | Logic bugs, broken error handling, missing tests on critical paths, N+1 queries |
| 🟡 **minor** | Passes + comment | DRY violations, unclear naming, missing public API docs, swallowed exceptions |
| 💡 **suggestion** | Passes | Style, optional refactors, micro-optimisations, personal preference |

The `severity_threshold` in `.paul.yml` controls where blocking starts. Default is `major` — meaning `critical` and `major` issues fail the check, while `minor` and `suggestion` let it through.

A file with changes that couldn't be reviewed (the LLM stayed unavailable, its response was cut off, GitHub didn't return the diff, or a time or cost budget ran out) also fails the check. The comment lists every such file with the reason. `on_incomplete: neutral` lets the check pass with a warning only when the LLM provider was unavailable: a PR's author could cause any of the other reasons on purpose, so those always fail.

---

## Prompt Caching

Paul lays out every request from its most stable part to its least, so all the calls in a run share one prefix:

| Layer | What it holds | Changes when | Cached for |
|-------|---------------|--------------|------------|
| 1. Rules | Paul's review rules and output format | Paul is upgraded | `cache.ttl` (5m or 1h) |
| 2. Repo | `CLAUDE.md`, `README.md`, `.agents/rules/*.md`, `custom_instructions`, `language` | they change on the base branch | `cache.ttl` |
| 3. PR | Title, description, changed-file list, every file's diff | every push | 5 minutes |
| 4. Task | The walkthrough, or one file to review with its current text | every call | not cached |

The walkthrough call writes layers 1–3 to the cache. Each file's review then reads them at a tenth of the input price and pays full price only for its own task and answer. Every call in a run uses the same model, effort and output schema, since changing any of them would start a new cache. The **Review details** section of the comment and the `cache_hit_ratio` output show how much input came from the cache.

- **Claude:** Paul sets the three cache breakpoints.
- **OpenAI:** prompts of 1,024+ tokens are cached automatically; Paul sends a `prompt_cache_key` per repository so a run's calls share one cache.
- **Gemini 2.5+:** cached implicitly.

When a PR's diff is too large to send with every call (`review.pr_context_max_tokens`, 80,000 tokens by default), layer 3 holds only each file's hunk headers, and each file's review brings its own diff.

---

## Configuration Reference (`.paul.yml`)

```yaml
# LLM provider: anthropic | openai | google
provider: anthropic

# A Claude model ID, or a LiteLLM model string for OpenAI and Google
model: claude-sonnet-5-5
effort: medium              # low | medium | high | xhigh | max; "" = the model's default
max_tokens: 16000           # per response (alias: max_output_tokens); capped at the model's limit
# temperature: 0            # optional (0-1 for Claude); never sent to models that reject it
# language: Spanish         # language of the review text; English if unset
concurrency: 4              # files reviewed at once (1-16)

# Minimum severity that fails the check
severity_threshold: major   # critical | major | minor

# When a file with changes couldn't be reviewed
on_incomplete: fail         # fail | neutral (neutral excuses only provider outages)

# When a fork or Dependabot PR has no API key (a missing key elsewhere always fails)
forks: skip                 # skip | fail

# Paul stops reviewing after this long and lists the files it didn't get to.
# Keep it below the job's timeout-minutes.
time_budget_minutes: 45

cache:
  ttl: 5m                   # 5m | 1h for the rules and repo layers (Claude)

review:
  diff_context: auto        # auto | full | compact: how much of the PR's diff every call sees
  pr_context_max_tokens: 80000

budget:
  max_files: 300            # files past this aren't reviewed, and are listed
  max_cost_usd: 5.0         # no new LLM call once the run's estimated cost reaches this (running calls finish)

# .gitignore-style patterns for files to skip, added to the built-in defaults
excluded_paths:
  - "**/migrations/**"
exclude_defaults: true      # false = use only your patterns

# Added to Paul's instructions: describe your stack and focus areas
custom_instructions: |
  This is a Node.js/Express API. Focus on:
  - Input validation on all route handlers
  - JWT verification before accessing protected resources
```

An unknown setting prints a warning; an invalid value fails the run and says which setting is wrong. [`.paul.yml.example`](.paul.yml.example) explains every option.

Paul also reads `CLAUDE.md`, `README.md` and `.agents/rules/*.md` from the base branch (up to 8,000 characters each) and gives them to the model as codebase context.

---

## PR Comment Format

Paul keeps **one summary comment** per PR and edits it on every push:

```
## Paul's Review

Verdict: 🔴 Changes needed: findings at or above the `major` threshold.
Coverage: 3 of 3 file(s) with changes reviewed · skipped: 1 matches excluded_paths

▸ 📋 Walkthrough (summary, changes table, severity overview)

▸ 🔴 [Critical] SQL injection in invoice search — `app/views.py:12`
     Impact / Description / Fix

✅ Resolved since the last review as of `abc1234`
- ~~Missing permission check~~ — `app/views.py:40`

🧪 Test Recommendations
▸ 🤖 Prompt for all issues
▸ Skipped files · Review details (config, model, commit, tokens incl. cache reads, estimated cost)
```

Files that couldn't be reviewed appear in a warning block at the top. Hidden markers let the next run find the comment and tell the model what it reported before. A finding shows as resolved only when the model says the new diff fixes it **and** that file's diff changed since the review that reported it; in an unchanged file it stays on record.

---

## Outputs

| Output | Value |
|--------|-------|
| `verdict` | `pass`, `block`, `fail` (files not reviewed), `neutral` (files not reviewed, passing), `skipped` (fork or Dependabot PR without a key) or `error` |
| `highest_severity` | `critical`, `major`, `minor` or `suggestion` |
| `findings` | Number of findings |
| `reviewed_files`, `skipped_files`, `failed_files` | File counts from the coverage ledger |
| `cost_usd` | Estimated cost of the run in USD (empty when Paul doesn't know the model's prices) |
| `cache_hit_ratio` | Share of input tokens read from the prompt cache, from 0 to 1 |
| `comment_url` | Link to Paul's summary comment |

```yaml
    steps:
      - uses: gmarte/PaulRudd@v2
        id: paul
        with:
          api_key: ${{ secrets.ANTHROPIC_API_KEY }}
      - if: always()
        env:
          VERDICT: ${{ steps.paul.outputs.verdict }}
          COST: ${{ steps.paul.outputs.cost_usd }}
        run: echo "Paul's verdict: $VERDICT (estimated cost: $COST USD)"
```

Paul also writes a short report (verdict, findings, files, cache use, cost) to the job's summary page.

---

## AI Agent Auto-Fix

Every finding can include an `autofix` object, and the comment ends with one prompt covering all findings:

```json
{
  "autofix": {
    "original": "cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')",
    "replacement": "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
  }
}
```

Paste the prompt into Claude Code or Cursor to apply the fixes.

---

## Repository Structure

```
PaulRudd/
├── action.yml                    # Composite action definition
├── requirements.in               # Direct dependencies (the Claude path)
├── requirements.lock             # Hash-locked dependencies the action installs
├── requirements-litellm.in       # LiteLLM, for OpenAI and Gemini
├── requirements-litellm.lock     # Hash-locked; installed only for those providers
├── .paul.yml                     # Config Paul uses to review its own PRs
├── .paul.yml.example             # Config template for consuming repos
├── prompts/
│   ├── static_rules.md           # Paul's rules: the cached layer every call starts with
│   ├── task_walkthrough.md       # Pass 1 task: summary + changes
│   └── task_review.md            # Pass 2 task: one file's findings
├── src/
│   ├── paul.py                   # Entrypoint: preflight, passes, gate, outputs
│   ├── config.py                 # .paul.yml loader (base branch)
│   ├── provider.py               # Tells action.yml whether to install LiteLLM
│   ├── diff_processor.py         # Changed files, path filters, PR diff context, patch splitting
│   ├── reviewer.py               # Prompt layers, tasks, parsing, normalization, gate
│   ├── llm.py                    # Provider-neutral calls: retries, budgets, usage and cost
│   ├── llm_anthropic.py          # Claude on the Anthropic SDK: cache breakpoints, structured outputs
│   ├── llm_litellm.py            # OpenAI and Gemini through LiteLLM
│   ├── schemas.py                # The JSON schema every answer follows
│   ├── render.py                 # PR comment, review and job summary rendering
│   ├── coverage.py               # Which files were reviewed, skipped or failed
│   └── github_client.py          # GitHub API interactions
├── tests/                        # pytest suite, including wire-level contract tests
├── examples/
│   └── paul-workflow.yml         # Template workflow for consuming repos
└── .github/workflows/
    ├── ci.yml                    # Tests
    └── self-review.yml           # Paul reviews its own PRs
```

---

## Development

```bash
pip install -r requirements-litellm.lock   # hash-checked; includes everything in requirements.lock
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no network access or API keys. `tests/test_contract.py` runs the real SDKs against a local server to pin down what goes over the wire (cache markers, output schema, effort, temperature), so run it after any SDK upgrade.

To update a dependency, edit `requirements.in` or `requirements-litellm.in` and regenerate both locks with the commands at the top of `requirements.in`.

---

## Cost

Each run makes one LLM call for the walkthrough plus one per changed file (more for very large files). The walkthrough writes the shared prefix to the cache; each file's review then reads it at a tenth of the input price and pays full price only for the file's own text and its answer. The **Review details** section and the `cost_usd` output show each run's estimated cost, and Paul starts no new call once a run reaches `budget.max_cost_usd` (5 USD by default).

---

## License

MIT
