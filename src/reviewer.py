"""
Builds the prompts for each pass and turns the model's answers into findings.

Every call shares one prompt prefix (see llm.PromptPlan): Paul's static rules,
the repo's guidelines and instructions, then the PR context. Only the task at
the end differs between calls:

  Pass 1 — walkthrough: summary + one line per changed file
  Pass 2 — review: one call per changed file (or per part of an oversized file)

Every call returns validated, normalized data or raises an llm.ReviewError, so
the caller records the file as not reviewed instead of treating it as clean.
"""

import functools
import json
import os
import re
import secrets
import unicodedata
from pathlib import Path

import llm
from diff_processor import neutralize, one_line
from schemas import CATEGORIES, CONFIDENCES

PROMPTS = Path(__file__).parent.parent / "prompts"

SEVERITY_ORDER = ["suggestion", "minor", "major", "critical"]

# Severity labels by the start of a word, after lower-casing and removing accents
# and decoration ("🔴 Critical" → "critical"). This also covers translations such as
# critique, crítico, mayor/majeur, menor/mineur, sugerencia. Anything else counts as
# critical, so an unexpected label can never slip under the blocking threshold.
_SEVERITY_PREFIXES = (
    ("crit", "critical"), ("block", "critical"), ("bloq", "critical"),
    ("maj", "major"), ("mayor", "major"), ("high", "major"), ("alt", "major"), ("grave", "major"),
    ("medi", "major"), ("moder", "major"),
    ("min", "minor"), ("menor", "minor"), ("low", "minor"), ("baj", "minor"),
    ("sug", "suggestion"), ("nit", "suggestion"), ("info", "suggestion"),
)

_API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
_MAX_PR_BODY_CHARS = 4000


# ── Prompt plan ──────────────────────────────────────────────────────────────

def build_plan(config: dict, pr_block: str) -> llm.PromptPlan:
    """The prefix shared by every call of the run."""
    return llm.PromptPlan(
        static_rules=_prompt("static_rules.md"),
        repo_context=_repo_context(
            config.get("repo_context", ""), config.get("custom_instructions", ""), config.get("language", "")
        ),
        pr_context=pr_block,
    )


def pr_context(title: str, body: str, file_table: str, diff_block: str) -> str:
    """
    The PR-wide part of every request: what the PR says it does, what it touches,
    and how. The author's title and description can't forge Paul's tags.
    """
    body = neutralize((body or "").strip()) or "(none)"
    if len(body) > _MAX_PR_BODY_CHARS:
        body = body[:_MAX_PR_BODY_CHARS] + "\n[description truncated]"
    return (
        f"<pr>\nTitle: {one_line(title or '') or '(none)'}\nDescription:\n{body}\n</pr>\n\n"
        f"<changed_files>\n{file_table}\n</changed_files>\n\n"
        f"{diff_block}"
    )


def walkthrough_task(diff_text: str | None = None, omitted: list = ()) -> str:
    """The walkthrough task. In compact mode the diffs come with it, since <diff> holds only hunk headers."""
    parts = [_prompt("task_walkthrough.md")]
    if diff_text is not None:
        parts.append(f"<diff_to_review>\n{diff_text}</diff_to_review>")
    if omitted:
        names = "\n".join(f"- {one_line(c.path)} (+{c.additions} -{c.deletions})" for c in omitted)
        parts.append(f"Diffs left out to fit the size budget (describe them from their names only):\n{names}")
    return "\n\n".join(parts)


def review_task(label: str, file_text: str | None, patch: str | None, prior_findings: list) -> str:
    """
    The review task for one file (or part of one). `patch` is included when the
    diff to review isn't already in <diff>: in compact mode, or for one part of
    an oversized file.
    """
    parts = [_prompt("task_review.md").format(label=one_line(label))]
    if prior_findings:
        lines = "\n".join(f"- [{f['severity']}] {_line_ref(f)}: {one_line(f['title'])}" for f in prior_findings)
        parts.append(f"<prior_findings>\nReported by Paul on an earlier commit of this PR:\n{lines}\n</prior_findings>")
    if file_text:
        parts.append(file_text)
    if patch is not None:
        parts.append(f"<diff_to_review>\n{neutralize(patch)}\n</diff_to_review>")
    return "\n\n".join(parts)


@functools.lru_cache(maxsize=8)
def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=8)
def _repo_context(repo_context: str, custom_instructions: str, language: str) -> str:
    """The repo-specific layer: guideline files, custom instructions, language. Empty when there's none."""
    parts = []
    if repo_context.strip():
        parts.append(
            "## Codebase Context\n\n"
            "The following files describe this repo's conventions and rules. "
            "Use them to avoid suggesting changes that conflict with established patterns.\n\n"
            f"{repo_context.strip()}"
        )
    if custom_instructions.strip():
        parts.append(f"## Repo-Specific Instructions\n\n{custom_instructions.strip()}")
    if language.strip():
        parts.append(
            "## Language\n\n"
            f"Write every human-readable string (summaries, titles, descriptions, impacts, fixes and "
            f"test recommendations) in {language.strip()}. Keep JSON keys, enum values and code unchanged."
        )
    return "\n\n".join(parts)


# ── Calls ────────────────────────────────────────────────────────────────────

def review_walkthrough(plan: llm.PromptPlan, config: dict) -> dict:
    """Pass 1: a summary and a one-line description per file. Returns {summary, changes[]}."""
    return _call_for_section(plan, config, "walkthrough", _normalize_walkthrough)


def review_file(file_path: str, plan: llm.PromptPlan, config: dict) -> dict:
    """
    Pass 2: issues in one file's diff, or one part of it.
    Returns {issues[], test_recommendations[], resolved_prior_findings[]}.
    """
    return _call_for_section(plan, config, "review", lambda data: normalize_file_review(data, file_path))


def _call_for_section(plan: llm.PromptPlan, config: dict, section: str, validate) -> dict:
    """Call the LLM and read its answer for one section, asking once more if the output is unusable."""
    for attempt in (1, 2):
        raw = llm.complete(plan, config)
        try:
            return validate(_section(_extract_json(raw), section))
        except llm.InvalidOutput as e:
            _debug(f"Unusable output:\n{raw}")
            if attempt == 2:
                raise
            llm.log(f"    Unusable output ({e}); asking again.")


def _section(data: dict, section: str) -> dict:
    """
    The envelope's section for this task. A model without structured outputs may
    answer with the section's contents directly, so that is accepted too.
    """
    if "task" in data or section in data:
        value = data.get(section)
        if not isinstance(value, dict):
            raise llm.InvalidOutput(f"the response has no '{section}' section")
        return value
    return data


# ── Gate ─────────────────────────────────────────────────────────────────────

def determines_outcome(overall_severity: str, threshold: str, complete: bool = True, on_incomplete: str = "fail") -> str:
    """
    The gate: 'block' for a finding at or above the threshold; otherwise 'fail' or
    'neutral' (per on_incomplete) if any file went unreviewed; otherwise 'pass'.
    """
    if SEVERITY_ORDER.index(overall_severity) >= SEVERITY_ORDER.index(threshold):
        return "block"
    if not complete:
        return "fail" if on_incomplete == "fail" else "neutral"
    return "pass"


def highest_severity(issues: list) -> str:
    if not issues:
        return "suggestion"
    return max((issue["severity"] for issue in issues), key=SEVERITY_ORDER.index)


def api_key_available(config: dict) -> bool:
    provider = config["provider"]
    return bool(
        os.environ.get("PAUL_API_KEY")
        or os.environ.get(_API_KEY_ENV[provider])
        or (provider == "google" and os.environ.get("GEMINI_API_KEY"))
    )


def set_api_key_env(config: dict) -> None:
    """Map the generic PAUL_API_KEY to the provider-specific env var the SDKs read."""
    api_key = os.environ.get("PAUL_API_KEY", "")
    if not api_key:
        return
    env_var = _API_KEY_ENV[config["provider"]]
    if not os.environ.get(env_var):
        os.environ[env_var] = api_key


def _debug(message: str) -> None:
    """
    Print only when the workflow runs with debug logging (RUNNER_DEBUG=1): raw model
    output can be long. It is untrusted, so workflow commands are off while it prints.
    """
    if os.environ.get("RUNNER_DEBUG") == "1":
        token = secrets.token_hex(16)
        print(f"::stop-commands::{token}")
        print(message)
        print(f"::{token}::")


# ── Parsing and normalization ────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """
    The JSON object in the response, tolerating code fences and prose around it.
    Prose that holds two objects is ambiguous (an echoed template next to the real
    answer, say), so it counts as unusable rather than trusting the first one.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        objects = []
        decoder = json.JSONDecoder()
        pos = 0
        while (start := text.find("{", pos)) != -1:
            try:
                obj, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                pos = start + 1
                continue
            objects.append(obj)
            pos = end
        if not objects:
            raise llm.InvalidOutput("no JSON object in the response")
        if len(objects) > 1:
            raise llm.InvalidOutput("more than one JSON object in the response")
        data = objects[0]
    if not isinstance(data, dict):
        raise llm.InvalidOutput("the response is not a JSON object")
    return data


def normalize_severity(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    for word in re.findall(r"[a-z]+", text):  # "Severity: high" → "high"
        for prefix, severity in _SEVERITY_PREFIXES:
            if word.startswith(prefix):
                return severity
    llm.log(f"    Unknown severity {value!r}; treating it as critical.")
    return "critical"


def normalize_file_review(data: dict, file_path: str) -> dict:
    if "issues" not in data:
        raise llm.InvalidOutput("the response has no 'issues' field")
    raw_issues = data.get("issues") or []
    if isinstance(raw_issues, dict):
        raw_issues = [raw_issues]
    if not isinstance(raw_issues, list):
        raise llm.InvalidOutput("'issues' is not a list")

    issues = []
    for item in raw_issues:
        # A finding written as a bare string, or an object with neither title nor
        # description (", " counts as neither), can't be shown or gated reliably:
        # ask for the review again.
        if not isinstance(item, dict):
            raise llm.InvalidOutput("an issue is not a JSON object")
        title, description = _as_text(item.get("title")).strip(), _as_text(item.get("description")).strip()
        if not _has_words(title) and not _has_words(description):
            raise llm.InvalidOutput("an issue has neither a title nor a description")
        suggestion = item.get("suggestion")
        if isinstance(suggestion, str):
            suggestion = {"explanation": suggestion}
        elif not isinstance(suggestion, dict):
            suggestion = {}
        evidence = _as_text(item.get("evidence"))
        replacement = suggestion.get("replacement")
        autofix = suggestion.get("autofix")
        if not isinstance(autofix, dict):
            autofix = {"original": evidence, "replacement": replacement} if evidence and isinstance(replacement, str) else None
        line_start = _as_int(item.get("line_start"))
        issues.append({
            "severity": normalize_severity(item.get("severity")),
            "file": file_path,  # from the request, never from the model
            "line_start": line_start,
            "line_end": _as_int(item.get("line_end")) or line_start,
            "title": title if _has_words(title) else _first_sentence(description),
            "description": description,
            "impact": _as_text(item.get("impact")),
            "evidence": evidence,
            "category": item.get("category") if item.get("category") in CATEGORIES else "correctness",
            "confidence": item.get("confidence") if item.get("confidence") in CONFIDENCES else "medium",
            "pre_existing": item.get("pre_existing") is True,
            "suggestion": {"explanation": _as_text(suggestion.get("explanation")), "autofix": autofix},
        })

    return {
        "issues": issues,
        "test_recommendations": _as_text_list(data.get("test_recommendations")),
        "resolved_prior_findings": _as_text_list(data.get("resolved_prior_findings")),
    }


def _normalize_walkthrough(data: dict) -> dict:
    if "summary" not in data and "changes" not in data:
        raise llm.InvalidOutput("the response has neither 'summary' nor 'changes'")
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []
    return {
        "summary": _as_text(data.get("summary")),
        "changes": [
            {"file": _as_text(c.get("file")), "summary": _as_text(c.get("summary"))}
            for c in changes if isinstance(c, dict)
        ],
    }


def _has_words(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _first_sentence(text: str, limit: int = 80) -> str:
    """A title made from the description, for a finding that came without a usable one."""
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def _as_int(value) -> int | None:
    """A positive line number, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON Infinity
        return None
    return number if number > 0 else None


def _as_text(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Drop lone surrogates, which can't be encoded as UTF-8 for the GitHub API.
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _as_text_list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [_as_text(item) for item in value if item not in (None, "")]


def _line_ref(finding: dict) -> str:
    start, end = finding.get("line_start"), finding.get("line_end")
    if start and end and start != end:
        return f"lines {start}-{end}"
    return f"line {start}" if start else "file"
