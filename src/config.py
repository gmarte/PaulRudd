"""
Loads and validates the .paul.yml configuration from the target repository workspace.
Falls back to safe defaults if the file is absent or a field is missing.
"""

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


def load_config() -> dict:
    config_path = os.environ.get("PAUL_CONFIG_PATH", ".paul.yml")

    config = dict(DEFAULTS)

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            user_config = yaml.safe_load(f) or {}
        config.update({k: v for k, v in user_config.items() if v is not None})

    _validate(config)
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
