# Paul 👨‍⚖️

> **Paul Rudd = PR.** A reusable GitHub Action that reviews pull requests using LLMs.

Paul is a self-hosted AI code reviewer you deploy once and reuse across all your repositories. Drop a workflow file into any repo, add an API key secret, and every PR gets an automated review with severity-gated approvals.

---

## Features

- **4-tier severity model** — `critical`, `major`, `minor`, `suggestion` with configurable blocking threshold
- **Multi-LLM support** — Anthropic Claude, OpenAI GPT, Google Gemini (via [LiteLLM](https://github.com/BerriAI/litellm))
- **Per-repo configuration** — `.paul.yml` controls the model, severity threshold, excluded paths, and custom review instructions
- **AI-agent-ready output** — every issue includes an `autofix` object (original + replacement) so tools like Claude Code or Cursor can apply fixes automatically
- **Structured PR comments** — severity table, per-issue impact + fix, test recommendations
- **Zero vendor lock-in** — swap providers by changing two lines in `.paul.yml`

---

## How It Works

```
PR opened / updated
        │
        ▼
  Fetch diff via GitHub API
        │
        ▼
  Filter excluded paths + truncate
        │
        ▼
  Call LLM (Claude / GPT / Gemini)
        │
        ▼
  Parse structured JSON response
        │
        ├─ Post formatted comment to PR
        ├─ Set commit status (success / failure)
        └─ Submit APPROVE or REQUEST_CHANGES
```

---

## Quick Start

### 1. Add Paul to a repo

Copy this workflow file to `.github/workflows/paul.yml` in the target repository:

```yaml
name: Paul PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  pull-requests: write
  contents: read
  statuses: write

jobs:
  paul:
    name: "Paul / AI PR Review"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: gmarte/PaulRudd@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### 2. Add your API key secret

In the target repo: **Settings → Secrets and variables → Actions → New repository secret**

| Provider | Secret name |
|----------|------------|
| Anthropic (default) | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GOOGLE_API_KEY` |

### 3. (Optional) Add a `.paul.yml` config

Copy `.paul.yml.example` to `.paul.yml` in the repo root:

```yaml
provider: anthropic
model: claude-sonnet-4-6
severity_threshold: major   # critical | major | minor

excluded_paths:
  - "migrations/**"
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

---

## Severity Model

| Severity | PR Outcome | Examples |
|----------|-----------|---------|
| 🔴 **critical** | Blocks — REQUEST_CHANGES | Security vulns, exposed secrets, SQL injection, auth bypass, data loss |
| 🟠 **major** | Blocks — REQUEST_CHANGES | Logic bugs, broken error handling, missing tests on critical paths, N+1 queries |
| 🟡 **minor** | Approves + comment | DRY violations, unclear naming, missing public API docs, swallowed exceptions |
| 💡 **suggestion** | Approves | Style, optional refactors, micro-optimisations, personal preference |

The `severity_threshold` in `.paul.yml` controls where blocking starts. Default is `major` — meaning `critical` and `major` issues block the PR, while `minor` and `suggestion` let it through.

---

## Configuration Reference (`.paul.yml`)

```yaml
# LLM provider: anthropic | openai | google
provider: anthropic

# Model — any LiteLLM-compatible model string for the provider
# Anthropic: claude-sonnet-4-6 | claude-opus-4-6 | claude-haiku-4-5-20251001
# OpenAI:    gpt-4o | gpt-4o-mini | o1-preview
# Google:    gemini-1.5-pro | gemini-1.5-flash
model: claude-sonnet-4-6

max_tokens: 4096
temperature: 0

# Minimum severity that blocks the PR
severity_threshold: major   # critical | major | minor

# Glob patterns for files to skip entirely
excluded_paths:
  - "**/*.lock"
  - "**/dist/**"
  - "**/*.min.js"

# Injected into Paul's system prompt — describe your stack and focus areas
custom_instructions: |
  This is a Node.js/Express API. Focus on:
  - Input validation on all route handlers
  - JWT verification before accessing protected resources
```

---

## PR Comment Format

```
## 👨‍⚖️ Paul's Review

**Summary:** This PR adds a new user authentication endpoint but passes user input directly to a SQL query.

| Severity | Count |
|----------|-------|
| 🔴 Critical | 1 |
| 🟠 Major | 0 |
| 🟡 Minor | 2 |
| 💡 Suggestion | 1 |

---

### 🔴 [Critical] SQL injection vulnerability — `src/api/auth.py:42`
**Impact:** An attacker can dump or corrupt the entire database.
**Description:** User input is interpolated directly into the query string.
**Fix:** Use parameterised queries instead of string interpolation.
```python
cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))
```

### 🧪 Test Recommendations
- Add a test that sends a malformed user_id and verifies a 400 response with no DB error.

---
*Powered by [Paul](https://github.com/gmarte/PaulRudd) · Model: `claude-sonnet-4-6`*
```

---

## AI Agent Auto-Fix

Every issue in Paul's JSON output includes an `autofix` object:

```json
{
  "autofix": {
    "type": "replace",
    "original": "cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')",
    "replacement": "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
  }
}
```

Tools like Claude Code or Cursor can parse this to apply fixes automatically without manual copy-paste.

---

## Repository Structure

```
PaulRudd/
├── action.yml                    # Composite action definition
├── requirements.txt              # litellm, requests, pyyaml
├── .paul.yml.example             # Config template for consuming repos
├── prompts/
│   └── system_prompt.md          # LLM system prompt — the review brain
├── src/
│   ├── paul.py                   # Main entrypoint
│   ├── config.py                 # .paul.yml loader
│   ├── diff_processor.py         # Diff fetching, filtering, truncation
│   ├── reviewer.py               # LiteLLM call + JSON parsing
│   └── github_client.py          # GitHub API interactions
└── .github/workflows/
    └── consumer-example.yml      # Template workflow for consuming repos
```

---

## Cost Estimate

| Provider | Model | Approx. cost/review |
|----------|-------|-------------------|
| Anthropic | claude-sonnet-4-6 | ~$0.02–$0.05 |
| OpenAI | gpt-4o | ~$0.02–$0.06 |
| Google | gemini-1.5-pro | ~$0.01–$0.03 |

At 100 PRs/month ≈ **$2–6/month** vs $30+/month for SaaS alternatives.

---

## License

MIT
