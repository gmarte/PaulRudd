"""
Calls the configured LLM via LiteLLM using a two-pass strategy:

  Pass 1 — Walkthrough prompt: summary + changes table (small, fast, always fits)
  Pass 2 — Issues prompt: one LLM call per changed file (each response is bounded)

This architecture eliminates JSON truncation errors caused by a single massive
response for large PRs.
"""

import json
import os
import re
import time
from pathlib import Path

import litellm

from diff_processor import split_into_file_blocks


WALKTHROUGH_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "walkthrough_prompt.md"
ISSUES_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "issues_prompt.md"

SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]


# ── LLM call with retry ──────────────────────────────────────────────────────

def _completion_with_backoff(**kwargs) -> object:
    """Call litellm.completion with exponential backoff on server/overload errors."""
    delays = [30, 60, 120]
    for attempt, delay in enumerate(delays, 1):
        try:
            return litellm.completion(**kwargs)
        except (litellm.exceptions.InternalServerError, litellm.exceptions.ServiceUnavailableError) as e:
            if attempt == len(delays):
                raise
            print(f"  API overloaded (attempt {attempt}/{len(delays)}), retrying in {delay}s...")
            time.sleep(delay)


# ── Public API ───────────────────────────────────────────────────────────────

def review_walkthrough(diff: str, config: dict) -> dict:
    """
    Pass 1: Ask the LLM for a high-level walkthrough only.
    Returns: { overall_severity, summary, changes[] }
    Response is always small (~500 tokens), never truncated.
    """
    system_prompt = _build_prompt(WALKTHROUGH_PROMPT_PATH, config)
    _set_api_key_env(config)
    model = _resolve_model(config)

    response = _completion_with_backoff(
        model=model,
        messages=[{"role": "user", "content": diff}],
        system=system_prompt,
        max_tokens=2048,  # Walkthrough schema is tiny; hard cap keeps cost low
        temperature=config["temperature"],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    return _parse_walkthrough(raw)


def review_file(file_path: str, file_diff: str, config: dict) -> dict:
    """
    Pass 2: Review a single file's diff for issues.
    Returns: { issues[], test_recommendations[] }
    Each call is bounded by the size of one file's diff.
    """
    system_prompt = _build_prompt(ISSUES_PROMPT_PATH, config)
    _set_api_key_env(config)
    model = _resolve_model(config)

    user_content = f"File: {file_path}\n\n{file_diff}"

    response = _completion_with_backoff(
        model=model,
        messages=[{"role": "user", "content": user_content}],
        system=system_prompt,
        max_tokens=config["max_tokens"],
        temperature=config["temperature"],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    return _parse_file_review(raw)


def review(diff: str, config: dict) -> dict:
    """
    Combined two-pass entry point. Returns the same merged dict shape as before
    so any caller that uses review() directly still works without changes.
    """
    walkthrough = review_walkthrough(diff, config)

    file_blocks = split_into_file_blocks(diff)
    all_issues = []
    all_test_recs = []

    for file_path, file_diff in file_blocks:
        print(f"  Reviewing {file_path}...")
        result = review_file(file_path, file_diff, config)
        all_issues.extend(result.get("issues", []))
        all_test_recs.extend(result.get("test_recommendations", []))

    # Recompute overall_severity from actual issues (more accurate than Pass 1 estimate)
    if all_issues:
        max_issue = max(
            all_issues,
            key=lambda i: SEVERITY_ORDER.index(i.get("severity", "suggestion")),
        )
        overall_severity = max_issue.get("severity", "suggestion")
    else:
        overall_severity = "suggestion"

    return {
        "overall_severity": overall_severity,
        "summary": walkthrough["summary"],
        "changes": walkthrough["changes"],
        "issues": all_issues,
        "test_recommendations": all_test_recs,
    }


def determines_outcome(overall_severity: str, threshold: str) -> str:
    """Returns 'block' or 'approve' based on severity vs configured threshold."""
    sev_index = SEVERITY_ORDER.index(overall_severity)
    threshold_index = SEVERITY_ORDER.index(threshold)
    return "block" if sev_index >= threshold_index else "approve"


# ── Internal helpers ─────────────────────────────────────────────────────────

def _build_prompt(path: Path, config: dict) -> str:
    template = path.read_text(encoding="utf-8")
    custom = config.get("custom_instructions", "").strip()
    injection = f"\n## Repo-Specific Instructions\n\n{custom}\n" if custom else ""
    return template.replace("{CUSTOM_INSTRUCTIONS}", injection)


def _resolve_model(config: dict) -> str:
    model = config["model"]
    provider = config["provider"]
    provider_prefixes = {"anthropic", "openai", "google", "azure", "cohere"}
    if "/" in model and model.split("/")[0] in provider_prefixes:
        return model
    provider_map = {"anthropic": "anthropic", "openai": "openai", "google": "gemini"}
    prefix = provider_map.get(provider, provider)
    return f"{prefix}/{model}"


def _set_api_key_env(config: dict) -> None:
    """Map the generic PAUL_API_KEY to the provider-specific env var LiteLLM expects."""
    api_key = os.environ.get("PAUL_API_KEY", "")
    if not api_key:
        return
    env_var_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    env_var = env_var_map.get(config["provider"], "ANTHROPIC_API_KEY")
    if not os.environ.get(env_var):
        os.environ[env_var] = api_key


def _parse_walkthrough(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Walkthrough LLM returned invalid JSON: {e}\n\nRaw:\n{raw}") from e
    for field in ("overall_severity", "summary", "changes"):
        if field not in data:
            raise ValueError(f"Walkthrough response missing required field: '{field}'")
    return data


def _parse_file_review(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        last_chars = cleaned[-50:].strip()
        truncated = not (last_chars.endswith("}") or last_chars.endswith("]"))
        if truncated:
            raise ValueError(
                f"File review response appears truncated (hit max_tokens limit). "
                f"Consider increasing max_tokens in .paul.yml or splitting very large files.\n"
                f"JSON error: {e}"
            ) from e
        raise ValueError(f"File review LLM returned invalid JSON: {e}\n\nRaw:\n{raw}") from e
    if "issues" not in data:
        raise ValueError("File review response missing required 'issues' field")
    return data
