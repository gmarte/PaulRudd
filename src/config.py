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
    "version": 2,
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "max_tokens": 16000,            # per response; max_output_tokens is accepted as an alias
    "temperature": None,            # only sent when set, and only to models that accept it
    "effort": "medium",             # low | medium | high | xhigh | max; one value per run
    "concurrency": 4,               # files reviewed at once
    "severity_threshold": "major",  # critical | major | minor
    "excluded_paths": [],
    "exclude_defaults": True,
    "custom_instructions": "",
    "language": "",                 # e.g. "Spanish"; empty leaves it to the model
    "on_incomplete": "fail",        # fail | neutral: what to do when files couldn't be reviewed
    "forks": "skip",                # skip | fail: fork/Dependabot PRs, which get no API key
    "time_budget_minutes": 45,      # stop and report before the job's timeout kills the run
    "cache": {
        "ttl": "5m",                # 5m | 1h for the rules and repo-context blocks
    },
    "review": {
        "diff_context": "auto",     # full | compact | auto: every call sees the whole PR diff when it fits
        "pr_context_max_tokens": 80000,
    },
    "budget": {
        "max_files": 300,           # files beyond this aren't reviewed (and are reported)
        "max_cost_usd": 5.0,        # no new LLM calls once the run's estimated cost reaches this
    },
}
_SECTIONS = ("cache", "review", "budget")
_ALIASES = {"max_output_tokens": "max_tokens"}

VALID_PROVIDERS = {"anthropic", "openai", "google"}
VALID_SEVERITIES = {"critical", "major", "minor"}
VALID_ON_INCOMPLETE = {"fail", "neutral"}
VALID_FORKS = {"skip", "fail"}
VALID_EFFORTS = {"", "low", "medium", "high", "xhigh", "max"}
VALID_TTLS = {"5m", "1h"}
VALID_DIFF_CONTEXT = {"auto", "full", "compact"}

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


def configured_provider() -> str:
    """Just the provider from .paul.yml, for the install step that decides whether LiteLLM is needed."""
    text = read_repo_file(os.environ.get("PAUL_CONFIG_PATH", ".paul.yml"))
    user_config = (yaml.safe_load(text) if text else None) or {}
    provider = user_config.get("provider") if isinstance(user_config, dict) else None
    return provider if provider in VALID_PROVIDERS else DEFAULTS["provider"]


def load_config() -> dict:
    config_path = os.environ.get("PAUL_CONFIG_PATH", ".paul.yml")

    config = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULTS.items()}

    text = read_repo_file(config_path)
    if text:
        user_config = yaml.safe_load(text) or {}
        if not isinstance(user_config, dict):
            raise ValueError(f"{config_path} must be a YAML mapping of settings")
        for alias, key in _ALIASES.items():
            if alias in user_config:
                user_config.setdefault(key, user_config.pop(alias))
        for key in sorted(set(user_config) - set(DEFAULTS)):
            print(f"::warning::Ignoring unknown setting '{key}' in {config_path}.")
        for key, value in user_config.items():
            if key not in DEFAULTS or value is None:
                continue
            if key in _SECTIONS:
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be a mapping of settings")
                for sub in sorted(set(value) - set(DEFAULTS[key])):
                    print(f"::warning::Ignoring unknown setting '{key}.{sub}' in {config_path}.")
                config[key].update({k: v for k, v in value.items() if k in DEFAULTS[key] and v is not None})
            else:
                config[key] = value

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

    if not isinstance(config["effort"], str) or config["effort"] not in VALID_EFFORTS:
        raise ValueError(f"effort must be one of: {', '.join(sorted(VALID_EFFORTS - {''}))}")

    if not _is_int(config["concurrency"]) or not 1 <= config["concurrency"] <= 16:
        raise ValueError("concurrency must be a whole number from 1 to 16")

    if config["cache"]["ttl"] not in VALID_TTLS:
        raise ValueError(f"cache.ttl must be one of: {', '.join(sorted(VALID_TTLS))}")

    review = config["review"]
    if review["diff_context"] not in VALID_DIFF_CONTEXT:
        raise ValueError(f"review.diff_context must be one of: {', '.join(sorted(VALID_DIFF_CONTEXT))}")
    if not _is_int(review["pr_context_max_tokens"]) or review["pr_context_max_tokens"] < 1000:
        raise ValueError("review.pr_context_max_tokens must be a whole number, at least 1000")

    budget = config["budget"]
    if not _is_int(budget["max_files"]) or budget["max_files"] < 1:
        raise ValueError("budget.max_files must be a whole number, at least 1")
    if isinstance(budget["max_cost_usd"], bool) or not isinstance(budget["max_cost_usd"], (int, float)) \
            or budget["max_cost_usd"] <= 0:
        raise ValueError("budget.max_cost_usd must be a positive number")

    temperature = config["temperature"]
    highest = 1 if config["provider"] == "anthropic" else 2  # the Claude API accepts 0-1
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= highest
    ):
        raise ValueError(f"temperature must be a number between 0 and {highest} for the {config['provider']} provider")

    if not isinstance(config["excluded_paths"], list) or not all(isinstance(p, str) for p in config["excluded_paths"]):
        raise ValueError("excluded_paths must be a list of glob patterns")

    if not isinstance(config["exclude_defaults"], bool):
        raise ValueError("exclude_defaults must be true or false")

    for key in ("custom_instructions", "language", "model"):
        if not isinstance(config[key], str):
            raise ValueError(f"{key} must be a string")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
