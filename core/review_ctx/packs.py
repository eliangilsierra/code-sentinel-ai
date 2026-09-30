"""Stack packs: detection of the stacks in a repository and deterministic routing of changes."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from review_ctx.gitdiff import FileChange, git

PACKS_ENV = "REVIEW_SQUAD_PACKS"
LENSES_ENV = "REVIEW_SQUAD_LENSES"
UNIVERSAL = "_universal"
MAX_CONTAINS_FILES = 5
MAX_CONTAINS_BYTES = 200_000
TRUST_LEVELS = ("untrusted-ok", "trusted-only")


class PackError(Exception):
    """Raised when a pack definition is malformed."""


@dataclass(frozen=True)
class Detect:
    files: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Trigger:
    label: str
    added: re.Pattern[str] | None = None
    paths: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    lenses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tool:
    id: str
    cmd: str
    trust: str


@dataclass(frozen=True)
class Pack:
    id: str
    detect: Detect = field(default_factory=Detect)
    always: tuple[str, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    tools: tuple[Tool, ...] = ()
    root: Path | None = None


@dataclass(frozen=True)
class Hit:
    """A file that activated a lens, and the rule that did it."""

    path: str
    why: str


@dataclass
class Routing:
    """Tags per file and the files, with reasons, that activate each lens."""

    tags: dict[str, list[str]] = field(default_factory=dict)
    lenses: dict[str, list[Hit]] = field(default_factory=dict)


def _packs_base(env: str, folder: str) -> Path:
    override = os.environ.get(env)
    if override:
        return Path(override)
    checkout = Path(__file__).resolve().parents[2] / folder
    if checkout.is_dir():
        return checkout
    return Path(str(resources.files("review_ctx").joinpath(folder)))


def packs_dir() -> Path:
    """Directory holding the packs: ``$REVIEW_SQUAD_PACKS``, the checkout or the installed copy."""
    return _packs_base(PACKS_ENV, "packs")


def lenses_dir() -> Path:
    """Directory holding the lens files, resolved like :func:`packs_dir`."""
    return _packs_base(LENSES_ENV, "lenses")


def load_pack(path: Path) -> Pack:
    """Read and validate one ``pack.yaml``."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackError(f"{path}: cannot read pack: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise PackError(f"{path}: 'id' is required")
    try:
        return Pack(
            id=data["id"],
            detect=_detect(data.get("detect") or {}),
            always=_strings(data.get("always"), "always"),
            triggers=tuple(_trigger(i, t) for i, t in enumerate(data.get("triggers") or [])),
            tools=tuple(_tool(t) for t in data.get("tools") or []),
            root=path.parent,
        )
    except PackError as error:
        raise PackError(f"{path}: {error}") from error


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PackError(f"'{name}' must be a list of strings")
    return tuple(value)


def _detect(data: dict[str, Any]) -> Detect:
    unknown = set(data) - {"files", "contains", "extensions"}
    if unknown:
        raise PackError(f"unknown detect keys: {', '.join(sorted(unknown))}")
    return Detect(
        files=_strings(data.get("files"), "detect.files"),
        contains=_strings(data.get("contains"), "detect.contains"),
        extensions=tuple(e.lower() for e in _strings(data.get("extensions"), "detect.extensions")),
    )


def _trigger(index: int, data: dict[str, Any]) -> Trigger:
    if not isinstance(data, dict):
        raise PackError(f"triggers[{index}] must be a mapping")
    unknown = set(data) - {"added", "paths", "tags", "lenses"}
    if unknown:
        raise PackError(f"triggers[{index}]: unknown keys: {', '.join(sorted(unknown))}")
    if "added" not in data and "paths" not in data:
        raise PackError(f"triggers[{index}] needs 'added' or 'paths'")
    pattern = None
    if "added" in data:
        try:
            pattern = re.compile(data["added"])
        except (re.error, TypeError) as error:
            raise PackError(f"triggers[{index}].added is not a valid regex: {error}") from error
    tags = _strings(data.get("tags"), f"triggers[{index}].tags")
    lenses = _strings(data.get("lenses"), f"triggers[{index}].lenses")
    if not tags and not lenses:
        raise PackError(f"triggers[{index}] needs 'tags' or 'lenses'")
    label = tags[0] if tags else lenses[0]
    return Trigger(
        label, pattern, _strings(data.get("paths"), f"triggers[{index}].paths"), tags, lenses
    )


def _tool(data: dict[str, Any]) -> Tool:
    if not isinstance(data, dict) or not {"id", "cmd", "trust"} <= set(data):
        raise PackError("each tool needs 'id', 'cmd' and 'trust'")
    if data["trust"] not in TRUST_LEVELS:
        raise PackError(f"tool trust must be one of {', '.join(TRUST_LEVELS)}")
    return Tool(data["id"], data["cmd"], data["trust"])


def load_packs(directory: Path | None = None) -> list[Pack]:
    """Every pack under ``directory`` (default: :func:`packs_dir`), universal pack first."""
    base = directory or packs_dir()
    packs = [load_pack(p) for p in sorted(base.glob("*/pack.yaml"))]
    return sorted(packs, key=lambda p: (p.id != UNIVERSAL, p.id))


@cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    """Match ``path`` against a glob: ``*`` stays in a directory, ``**`` crosses directories."""
    return bool(_glob_regex(pattern).match(PurePosixPath(path).as_posix()))


def detect_packs(root: Path, packs: list[Pack], changed: list[str]) -> list[Pack]:
    """Packs whose detection rules hold for the repository at ``root`` and the changed paths."""
    tracked = git(root, "ls-files").splitlines()
    return [
        pack for pack in packs if pack.id == UNIVERSAL or _detected(root, pack, tracked, changed)
    ]


def _detected(root: Path, pack: Pack, tracked: list[str], changed: list[str]) -> bool:
    detect = pack.detect
    if not (detect.files or detect.contains or detect.extensions):
        return False
    if detect.extensions and not any(
        PurePosixPath(p).suffix.lower() in detect.extensions for p in changed
    ):
        return False
    if detect.files:
        matched = [t for t in tracked if any(_file_pattern_matches(p, t) for p in detect.files)]
        if not matched:
            return False
        if detect.contains and not _any_contains(root, matched, detect.contains):
            return False
    elif detect.contains:
        return False
    return True


def _file_pattern_matches(pattern: str, path: str) -> bool:
    if "/" in pattern:
        return glob_match(pattern, path)
    return fnmatch.fnmatch(PurePosixPath(path).name, pattern)


def _any_contains(root: Path, files: list[str], needles: tuple[str, ...]) -> bool:
    for path in files[:MAX_CONTAINS_FILES]:
        try:
            with (root / path).open(encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_CONTAINS_BYTES)
        except OSError:
            continue
        if any(needle in text for needle in needles):
            return True
    return False


def route(packs: list[Pack], files: list[FileChange]) -> Routing:
    """Apply the triggers of ``packs`` to the changed files, recording why each lens is active."""
    routing = Routing()
    file_tags: dict[str, set[str]] = {change.path: set() for change in files}
    for pack in packs:
        for lens in pack.always:
            for change in files:
                routing.lenses.setdefault(lens, []).append(Hit(change.path, f"{pack.id}:always"))
        for trigger in pack.triggers:
            for change in files:
                line = _trigger_line(trigger, change)
                if line is None:
                    continue
                file_tags[change.path].update(trigger.tags)
                place = f"{change.path}:{line}" if line else change.path
                hit = Hit(change.path, f"{pack.id}:{trigger.label}@{place}")
                for lens in trigger.lenses:
                    routing.lenses.setdefault(lens, []).append(hit)
    routing.tags = {path: sorted(tags) for path, tags in file_tags.items()}
    return routing


def _trigger_line(trigger: Trigger, change: FileChange) -> int | None:
    """Line that fires ``trigger`` in ``change``: 0 for a path-only match, ``None`` for no match."""
    if trigger.paths and not any(glob_match(p, change.path) for p in trigger.paths):
        return None
    if trigger.added is None:
        return 0
    for hunk in change.hunks:
        for number, text in hunk.added:
            if trigger.added.search(text):
                return number
    return None
