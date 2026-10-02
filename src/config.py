"""
Loads and validates the .paul.yml configuration for the repository under review.

In a pull-request run, .paul.yml and the guideline files are read from the PR's
base commit through the GitHub API, so a PR can't change the rules it is
reviewed by. Outside a PR (local runs, no BASE_SHA) they are read from the
working directory. Missing fields fall back to DEFAULTS.
"""

import os

import yaml

import github_client

# Files that are never worth an LLM call, excluded unless exclude_defaults is
# false. A user's excluded_paths are added to these.
#
# File-name patterns match a file's own name at any depth. They are kept apart
# from the directory patterns on purpose: as a gitignore pattern, "*.lock" would
# also match a directory named "payments.lock/" and hide the code inside it.
DEFAULT_EXCLUDED_FILES = [
    "*.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "go.sum",
    "*.pyc",
    "*.min.js",
    "*.min.css",
]
# Build-output directories, as gitignore patterns (any depth).
DEFAULT_EXCLUDED_DIRS = [
    "dist/",
    "build/",
    "__pycache__/",
]

DEFAULTS = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "max_tokens": 16000,
    "temperature": None,            # only sent when set, and only to models that accept it
    "severity_threshold": "major",  # critical | major | minor
    "excluded_paths": [],
    "exclude_defaults": True,
    "custom_instructions": "",
    "language": "",                 # e.g. "Spanish"; empty leaves it to the model
    "on_incomplete": "fail",        # fail | neutral: what to do when files couldn't be reviewed
    "forks": "skip",                # skip | fail: fork/Dependabot PRs, which get no API key
    "time_budget_minutes": 45,      # stop and report before the job's timeout kills the run
}

VALID_PROVIDERS = {"anthropic", "openai", "google"}
VALID_SEVERITIES = {"critical", "major", "minor"}
VALID_ON_INCOMPLETE = {"fail", "neutral"}
VALID_FORKS = {"skip", "fail"}

GUIDELINE_FILES = ["CLAUDE.md", "README.md"]
AGENTS_RULES_DIR = ".agents/rules"
_MAX_FILE_CHARS = 8000


def read_repo_file(path: str) -> str | None:
    """A file from the trusted copy of the repo: the base commit in a PR run, else the working directory."""
    base_sha = os.environ.get("BASE_SHA")
    if base_sha:
        return github_client.get_file_at(path, base_sha)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _list_repo_dir(path: str) -> list:
    base_sha = os.environ.get("BASE_SHA")
    if base_sha:
        return github_client.list_dir_at(path, base_sha)
    if not os.path.isdir(path):
        return []
    return [name for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))]


def read_repo_context() -> str:
    """CLAUDE.md, README.md and .agents/rules/*.md from the trusted copy of the repo, each capped."""
    rules = sorted(name for name in _list_repo_dir(AGENTS_RULES_DIR) if name.endswith(".md"))
    paths = GUIDELINE_FILES + [f"{AGENTS_RULES_DIR}/{name}" for name in rules]
    parts = []
    for path in paths:
        content = read_repo_file(path)
        if content:
            parts.append(f"### {path}\n\n{content[:_MAX_FILE_CHARS]}")
    return "\n\n---\n\n".join(parts)


def is_trusted_path(path: str, config_path: str) -> bool:
    """True for files Paul reads from the base commit: the config and the guideline files."""
    if path == config_path or path in GUIDELINE_FILES:
        return True
    rules_prefix = f"{AGENTS_RULES_DIR}/"
    return path.startswith(rules_prefix) and path.endswith(".md") and "/" not in path[len(rules_prefix):]


def load_config() -> dict:
    config_path = os.environ.get("PAUL_CONFIG_PATH", ".paul.yml")

    config = dict(DEFAULTS)

    text = read_repo_file(config_path)
    if text:
        user_config = yaml.safe_load(text) or {}
        if not isinstance(user_config, dict):
            raise ValueError(f"{config_path} must be a YAML mapping of settings")
        for key in sorted(set(user_config) - set(DEFAULTS)):
            print(f"::warning::Ignoring unknown setting '{key}' in {config_path}.")
        config.update({k: v for k, v in user_config.items() if k in DEFAULTS and v is not None})

    _normalize(config)
    _validate(config)
    config["config_path"] = config_path
    config["config_source"] = _describe_source(config_path, found=bool(text))
    config["repo_context"] = read_repo_context()
    return config


def _describe_source(config_path: str, found: bool) -> str:
    if not found:
        return f"defaults (no {config_path})"
    base_sha = os.environ.get("BASE_SHA")
    return f"{config_path}@{base_sha[:7]}" if base_sha else f"{config_path} (working directory)"


def _normalize(config: dict) -> None:
    # A YAML list of instructions reads naturally; treat it as one line per item.
    if isinstance(config["custom_instructions"], list):
        config["custom_instructions"] = "\n".join(str(item) for item in config["custom_instructions"])
    if isinstance(config["excluded_paths"], str):
        config["excluded_paths"] = [config["excluded_paths"]]


def _validate(config: dict) -> None:
    for key, valid in (
        ("provider", VALID_PROVIDERS),
        ("severity_threshold", VALID_SEVERITIES),
        ("on_incomplete", VALID_ON_INCOMPLETE),
        ("forks", VALID_FORKS),
    ):
        value = config[key]
        if not isinstance(value, str) or value not in valid:
            raise ValueError(f"Invalid {key} {value!r}. Must be one of: {', '.join(sorted(valid))}")

    if not _is_int(config["max_tokens"]) or config["max_tokens"] < 256:
        raise ValueError("max_tokens must be an integer >= 256")

    if not _is_int(config["time_budget_minutes"]) or config["time_budget_minutes"] < 1:
        raise ValueError("time_budget_minutes must be a whole number of minutes, at least 1")

    temperature = config["temperature"]
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2
    ):
        raise ValueError("temperature must be a number between 0 and 2")

    if not isinstance(config["excluded_paths"], list) or not all(isinstance(p, str) for p in config["excluded_paths"]):
        raise ValueError("excluded_paths must be a list of glob patterns")

    if not isinstance(config["exclude_defaults"], bool):
        raise ValueError("exclude_defaults must be true or false")

    for key in ("custom_instructions", "language", "model"):
        if not isinstance(config[key], str):
            raise ValueError(f"{key} must be a string")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
