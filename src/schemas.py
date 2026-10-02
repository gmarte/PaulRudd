"""
The JSON schema every LLM call answers with.

All passes (walkthrough, review, verify) share one envelope schema: when
structured outputs are on, the schema is part of what the provider caches, so a
single schema lets every call in a run read the same cached prompt prefix.
Each call fills the section named by its task and sets the other two to null.

The schema follows the structured-outputs rules: every object lists all its
properties as required, sets additionalProperties to false, and uses no
numeric or length constraints. Optional values are written as anyOf [X, null].
"""

SEVERITIES = ["critical", "major", "minor", "suggestion"]
CONFIDENCES = ["high", "medium", "low"]
CATEGORIES = [
    "security", "correctness", "reliability", "data_integrity",
    "performance", "maintainability", "testing", "docs",
]


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}

# Field order is generation order: the evidence and reasoning come before the
# verdict-like fields (severity, confidence), so the model decides with them in view.
FINDING = _object({
    "line_start": {"type": "integer"},
    "line_end": {"type": "integer"},
    "evidence": _STRING,
    "title": _STRING,
    "description": _STRING,
    "impact": _STRING,
    "category": {"type": "string", "enum": CATEGORIES},
    "pre_existing": {"type": "boolean"},
    "severity": {"type": "string", "enum": SEVERITIES},
    "confidence": {"type": "string", "enum": CONFIDENCES},
    "suggestion": _object({
        "explanation": _STRING,
        "replacement": _nullable(_STRING),
    }),
})

WALKTHROUGH = _object({
    "summary": _STRING,
    "changes": {"type": "array", "items": _object({"file": _STRING, "summary": _STRING})},
})

FILE_REVIEW = _object({
    "issues": {"type": "array", "items": FINDING},
    "test_recommendations": _STRINGS,
    "resolved_prior_findings": _STRINGS,
})

VERDICT = _object({
    "index": {"type": "integer"},
    "reason": _STRING,
    "verdict": {"type": "string", "enum": ["confirmed", "refuted", "uncertain"]},
    "severity": {"type": "string", "enum": SEVERITIES},
    "confidence": {"type": "string", "enum": CONFIDENCES},
    "line_start": {"type": "integer"},
    "line_end": {"type": "integer"},
})

ENVELOPE = _object({
    "task": {"type": "string", "enum": ["walkthrough", "review", "verify"]},
    "walkthrough": _nullable(WALKTHROUGH),
    "review": _nullable(FILE_REVIEW),
    "verification": _nullable({"type": "array", "items": VERDICT}),
})
