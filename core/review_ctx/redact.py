"""Removal of credential-like strings from text before it leaves the tool."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passwd|password|pwd)\b(\s*[:=]\s*)([\"']?)([^\s\"']{6,})\3"
)


def redact(text: str) -> str:
    """Replace private keys, well-known token formats and secret assignments with a marker."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", text
    )


def redact_data(value: Any) -> Any:
    """Return a copy of a JSON-compatible structure with every string redacted."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value
