"""Conservative secrets scrubbing, applied before steering evidence is persisted
to the skill spool or sent to the distiller LLM.

Heuristic by design — it favours over-redaction of obvious secret shapes over
letting a token reach disk or an external process. It is not a substitute for
keeping secrets out of prompts and files in the first place.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        # key = value / key: value. The value alternatives matter: a bare \S+
        # stops at the first space, so "Authorization: Bearer <token>" would
        # otherwise redact only the word "Bearer". The key may be quoted (JSON).
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization)"
            r"[\"']?\s*[=:]\s*"
            r"(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic|Token)\s+\S+|\S+)"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]{10,}\b"), "[REDACTED-JWT]"),
    # 48+ hex chars: long enough to spare 40-char git SHAs.
    (re.compile(r"\b[A-Fa-f0-9]{48,}\b"), "[REDACTED-HEX]"),
]


def scrub(text: str) -> str:
    """Redact obvious secret shapes from text. Idempotent enough to run twice."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
