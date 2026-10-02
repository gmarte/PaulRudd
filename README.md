# Paul 👨‍⚖️

> **Paul Rudd = PR.** A reusable GitHub Action that reviews pull requests using LLMs.

Paul is a self-hosted AI code reviewer you deploy once and reuse across all your repositories. Drop a workflow file into any repo, add an API key secret, and every PR gets an automated review with a severity-gated status check.

---

## Features

- **4-tier severity model** — `critical`, `major`, `minor`, `suggestion` with a configurable blocking threshold
- **A gate that fails closed** — every changed file is reviewed or listed with the reason it wasn't; a file that couldn't be reviewed never counts as clean
- **Multi-LLM support** — Anthropic Claude, OpenAI GPT, Google Gemini (via [LiteLLM](https://github.com/BerriAI/litellm))
- **Prompt caching** — on Anthropic, the system prompt is cached after the first file, so later files read it at a tenth of the input price
- **Per-repo configuration** — `.paul.yml` controls the model, severity threshold, excluded paths, language and custom review instructions. It is read from the base branch, so a PR can't change the rules it is reviewed by (see [Hardening the gate](#hardening-the-gate) to protect the workflow file too)
- **AI-agent-ready output** — a ready-to-paste prompt covering every finding, with `autofix` replacements where possible
- **Zero vendor lock-in** — swap providers by changing two lines in `.paul.yml`

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
  Pass 1: walkthrough (summary + changes) → summary comment posted right away
        │
        ▼
  Pass 2: one LLM call per file (large files in parts) → findings
        │
        ▼
  Update the summary comment · submit a COMMENT review
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
model: claude-sonnet-4-6
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

A file with changes that couldn't be reviewed (the LLM stayed unavailable, its response was cut off, or GitHub didn't return the diff) also fails the check, unless `on_incomplete: neutral`. The comment lists every such file with the reason.

---

## Configuration Reference (`.paul.yml`)

```yaml
# LLM provider: anthropic | openai | google
provider: anthropic

# Model — any LiteLLM-compatible model string for the provider
model: claude-sonnet-4-6

max_tokens: 16000           # per response; capped at the model's limit
# temperature: 0            # optional; never sent to models that reject it
# language: Spanish         # language of the review text; English if unset

# Minimum severity that fails the check
severity_threshold: major   # critical | major | minor

# When a file with changes couldn't be reviewed
on_incomplete: fail         # fail | neutral

# When a fork or Dependabot PR has no API key (a missing key elsewhere always fails)
forks: skip                 # skip | fail

# Paul stops reviewing after this long and lists the files it didn't get to.
# Keep it below the job's timeout-minutes.
time_budget_minutes: 45

# .gitignore-style patterns for files to skip, added to the built-in defaults
excluded_paths:
  - "**/migrations/**"
exclude_defaults: true      # false = use only your patterns

# Injected into Paul's system prompt — describe your stack and focus areas
custom_instructions: |
  This is a Node.js/Express API. Focus on:
  - Input validation on all route handlers
  - JWT verification before accessing protected resources
```

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
▸ Skipped files · Review details (config, model, commit, tokens incl. cache reads)
```

Files that couldn't be reviewed appear in a warning block at the top. Hidden markers let the next run find the comment and tell the model what it reported before.

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
├── requirements.in               # Direct dependencies
├── requirements.lock             # Hash-locked dependencies the action installs
├── .paul.yml                     # Config Paul uses to review its own PRs
├── .paul.yml.example             # Config template for consuming repos
├── prompts/
│   ├── walkthrough_prompt.md     # Pass 1: summary + changes
│   └── issues_prompt.md          # Pass 2: per-file findings
├── src/
│   ├── paul.py                   # Entrypoint: preflight, passes, gate
│   ├── config.py                 # .paul.yml loader (base branch)
│   ├── diff_processor.py         # Changed files, path filters, patch splitting
│   ├── reviewer.py               # LiteLLM calls, retries, parsing
│   ├── render.py                 # PR comment rendering
│   ├── coverage.py               # Which files were reviewed, skipped or failed
│   └── github_client.py          # GitHub API interactions
├── tests/                        # pytest suite
├── examples/
│   └── paul-workflow.yml         # Template workflow for consuming repos
└── .github/workflows/
    ├── ci.yml                    # Tests
    └── self-review.yml           # Paul reviews its own PRs
```

---

## Development

```bash
pip install -r requirements.lock      # hash-checked
pip install -r requirements-dev.txt
python -m pytest
```

To update a dependency, edit `requirements.in` and regenerate the lock with `uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.lock`.

---

## Cost

Each run makes one LLM call for the walkthrough plus one per changed file (more for very large files). As a rough guide, expect about $0.05–0.10 per changed file on `claude-sonnet-4-6`, less when the prompt cache is warm. Every review's **Review details** section shows the actual tokens used, including how many were read from the cache.

---

## License

MIT
