"""Repository review rules taken from ``REVIEW.md`` and ``CLAUDE.md`` as they exist on the base."""

from __future__ import annotations

import re
from pathlib import Path

from review_ctx.gitdiff import git

DOCUMENTS = ("REVIEW.md", "CLAUDE.md")
MAX_RULE_CHARS = 300
MAX_TOTAL_CHARS = 6000
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SLUG = re.compile(r"[^a-z0-9]+")


def base_document(root: Path, base: str, name: str) -> str | None:
    """Content of ``name`` at the root of commit ``base``, or ``None`` if it does not exist."""
    text = git(root, "show", f"{base}:{name}", check=False)
    return text if text.strip() else None


def slug(heading: str) -> str:
    """Lowercase, hyphen-separated form of a heading."""
    return _SLUG.sub("-", heading.lower()).strip("-") or "rules"


def extract_rules(name: str, text: str) -> list[str]:
    """Turn a Markdown document into ``NAME#section: rule`` strings, one per item or paragraph."""
    rules: list[str] = []
    section = "rules"
    current: list[str] = []
    in_fence = False

    def flush() -> None:
        if current:
            body = " ".join(part.strip() for part in current).strip()
            if body:
                rules.append(f"{name}#{section}: {_shorten(body)}")
            current.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
        elif in_fence:
            continue
        elif heading := _HEADING.match(line):
            flush()
            section = slug(heading.group(1))
        elif item := _ITEM.match(line):
            flush()
            current.append(item.group(1))
        elif not line.strip():
            flush()
        else:
            current.append(line)
    flush()
    return rules


def _shorten(text: str) -> str:
    return text if len(text) <= MAX_RULE_CHARS else text[: MAX_RULE_CHARS - 3] + "..."


def rules_for(root: Path, base: str) -> list[str]:
    """Rules of the repository as of ``base``; the working tree is never read.

    Reading the base protects the review from a change that rewrites its own rules.
    """
    rules: list[str] = []
    used = 0
    for name in DOCUMENTS:
        text = base_document(root, base, name)
        if text is None:
            continue
        for rule in extract_rules(name, text):
            if used + len(rule) > MAX_TOTAL_CHARS:
                return [*rules, f"{name}#truncated: further rules omitted"]
            rules.append(rule)
            used += len(rule)
    return rules
