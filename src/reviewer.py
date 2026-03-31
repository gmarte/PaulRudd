"""
Calls the configured LLM via LiteLLM and parses the structured JSON review response.
"""

import json
import os
import re
from pathlib import Path

import litellm


PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system_prompt.md"

SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]


def review(diff: str, config: dict) -> dict:
    system_prompt = _build_system_prompt(config)
    _set_api_key_env(config)

    model = _resolve_model(config)

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": diff}],
        system=system_prompt,
        max_tokens=config["max_tokens"],
        temperature=config["temperature"],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    return _parse_response(raw)


def _build_system_prompt(config: dict) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")

    custom = config.get("custom_instructions", "").strip()
    if custom:
        injection = f"\n## Repo-Specific Instructions\n\n{custom}\n"
    else:
        injection = ""

    return template.replace("{CUSTOM_INSTRUCTIONS}", injection)


def _resolve_model(config: dict) -> str:
    """
    Returns a LiteLLM-compatible model string.
    If the model already has a provider prefix (e.g. 'anthropic/...'), use as-is.
    Otherwise prepend the provider.
    """
    model = config["model"]
    provider = config["provider"]

    provider_prefixes = {"anthropic", "openai", "google", "azure", "cohere"}
    if "/" in model and model.split("/")[0] in provider_prefixes:
        return model

    provider_map = {
        "anthropic": "anthropic",
        "openai": "openai",
        "google": "gemini",
    }
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


def _parse_response(raw: str) -> dict:
    # Strip markdown code fences if the model wrapped the JSON anyway
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}\n\nRaw response:\n{raw}") from e

    _validate_response(data)
    return data


def _validate_response(data: dict) -> None:
    required_fields = {"overall_severity", "summary", "issues"}
    missing = required_fields - data.keys()
    if missing:
        raise ValueError(f"LLM response missing required fields: {missing}")

    valid_severities = set(SEVERITY_ORDER)
    if data["overall_severity"] not in valid_severities:
        raise ValueError(f"Invalid overall_severity: '{data['overall_severity']}'")

    for i, issue in enumerate(data.get("issues", [])):
        for field in ("severity", "file", "title", "description"):
            if field not in issue:
                raise ValueError(f"Issue #{i} missing required field: '{field}'")
        if issue["severity"] not in valid_severities:
            raise ValueError(f"Issue #{i} has invalid severity: '{issue['severity']}'")


def determines_outcome(overall_severity: str, threshold: str) -> str:
    """Returns 'block' or 'approve' based on severity vs configured threshold."""
    sev_index = SEVERITY_ORDER.index(overall_severity)
    threshold_index = SEVERITY_ORDER.index(threshold)
    return "block" if sev_index >= threshold_index else "approve"
