"""
Loads and validates the .paul.yml configuration from the target repository workspace.
Falls back to safe defaults if the file is absent or a field is missing.
"""

import glob as glob_module
import os
import yaml

DEFAULTS = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "max_tokens": 32768,
    "temperature": 0,
    "severity_threshold": "major",  # critical | major | minor
    "excluded_paths": [
        "**/*.lock",
        "**/package-lock.json",
        "**/yarn.lock",
        "**/Pipfile.lock",
        "**/poetry.lock",
        "**/go.sum",
        "**/dist/**",
        "**/build/**",
        "**/__pycache__/**",
        "**/*.pyc",
        "**/*.min.js",
        "**/*.min.css",
    ],
    "custom_instructions": "",
}

VALID_PROVIDERS = {"anthropic", "openai", "google"}
VALID_SEVERITIES = {"critical", "major", "minor"}

_CONTEXT_FILES = ["CLAUDE.md", "README.md"]
_AGENTS_RULES_GLOB = ".agents/rules/*.md"
_MAX_FILE_CHARS = 8000


def read_repo_context() -> str:
    """Read CLAUDE.md, README.md, and .agents/rules/*.md from the workspace."""
    parts = []

    for fname in _CONTEXT_FILES:
        if os.path.exists(fname):
            content = open(fname, encoding="utf-8").read(_MAX_FILE_CHARS)
            parts.append(f"### {fname}\n\n{content}")

    for fpath in sorted(glob_module.glob(_AGENTS_RULES_GLOB)):
        content = open(fpath, encoding="utf-8").read(_MAX_FILE_CHARS)
        parts.append(f"### {fpath}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def load_config() -> dict:
    config_path = os.environ.get("PAUL_CONFIG_PATH", ".paul.yml")

    config = dict(DEFAULTS)

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            user_config = yaml.safe_load(f) or {}
        config.update({k: v for k, v in user_config.items() if v is not None})

    _validate(config)
    config["repo_context"] = read_repo_context()
    return config


def _validate(config: dict) -> None:
    if config["provider"] not in VALID_PROVIDERS:
        raise ValueError(
            f"Invalid provider '{config['provider']}'. "
            f"Must be one of: {', '.join(sorted(VALID_PROVIDERS))}"
        )

    if config["severity_threshold"] not in VALID_SEVERITIES:
        raise ValueError(
            f"Invalid severity_threshold '{config['severity_threshold']}'. "
            f"Must be one of: {', '.join(sorted(VALID_SEVERITIES))}"
        )

    if not isinstance(config["max_tokens"], int) or config["max_tokens"] < 256:
        raise ValueError("max_tokens must be an integer >= 256")

    if not isinstance(config["excluded_paths"], list):
        raise ValueError("excluded_paths must be a list of glob patterns")
