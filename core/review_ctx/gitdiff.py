"""Reading of the change under review from git: files, statuses, line counts and changed lines."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DEFAULT_BRANCHES = ("origin/main", "origin/master", "main", "master")
STATUSES = ("A", "M", "D", "R")


class GitError(Exception):
    """Raised when git cannot produce the requested diff."""


@dataclass
class Hunk:
    """A run of changed lines: added lines with their new line numbers, removed lines as text.

    ``anchor`` is the new-file line before which the removed lines sat.
    """

    anchor: int
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    status: str
    old_path: str | None = None
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    def added_lines(self) -> list[int]:
        return [number for hunk in self.hunks for number, _ in hunk.added]


def git(root: Path, *args: str, check: bool = True) -> str:
    """Run git in ``root`` and return its standard output."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def default_base(root: Path) -> str:
    """Commit the local changes are measured against.

    The merge base of HEAD with its upstream branch, else with the first existing default branch,
    else HEAD itself so that only uncommitted work is reviewed.
    """
    candidates = ["@{upstream}", *DEFAULT_BRANCHES]
    for candidate in candidates:
        probe = subprocess.run(
            ["git", "merge-base", "HEAD", candidate],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip()
    return git(root, "rev-parse", "HEAD").strip()


def changes(root: Path, base: str | None = None, head: str | None = None) -> list[FileChange]:
    """Files changed between ``base`` and ``head``.

    With ``head`` the diff is ``base...head`` (changes since their merge base). Without ``head``
    the diff is ``base`` against the working tree, including staged, unstaged and untracked files,
    and ``base`` defaults to :func:`default_base`.
    """
    if head is not None:
        if base is None:
            raise GitError("a head commit requires a base commit")
        text = git(root, "diff", "--no-color", "--no-ext-diff", "-M", "-U0", f"{base}...{head}")
        return parse_diff(text)
    reference = base or default_base(root)
    text = git(root, "diff", "--no-color", "--no-ext-diff", "-M", "-U0", reference)
    files = parse_diff(text)
    known = {change.path for change in files}
    for path in git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if path and path not in known:
            files.append(_untracked(root, path))
    return sorted(files, key=lambda change: change.path)


def parse_diff(text: str) -> list[FileChange]:
    """Parse the output of ``git diff -U0`` into file changes."""
    files: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    new_line = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = FileChange(path=_path_from_header(line), status="M")
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.status = "A"
        elif line.startswith("deleted file mode"):
            current.status = "D"
        elif line.startswith("rename from "):
            current.status, current.old_path = "R", line.removeprefix("rename from ")
        elif line.startswith("rename to "):
            current.path = line.removeprefix("rename to ")
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("+++ "):
            target = line.removeprefix("+++ ").rstrip("	")
            if target != "/dev/null" and current.status != "R":
                current.path = target.removeprefix("b/")
        elif line.startswith("--- ") and current.status == "D":
            current.path = line.removeprefix("--- ").rstrip("	").removeprefix("a/")
        elif match := HUNK_HEADER.match(line):
            start, count = int(match.group(3)), int(match.group(4) or "1")
            hunk = Hunk(anchor=start if count else start + 1)
            new_line = start
            current.hunks.append(hunk)
        elif hunk is not None and line.startswith("+"):
            hunk.added.append((new_line, line[1:]))
            new_line += 1
            current.additions += 1
        elif hunk is not None and line.startswith("-"):
            hunk.removed.append(line[1:])
            current.deletions += 1
    return files


def _path_from_header(line: str) -> str:
    match = re.match(r"^diff --git a/(.*) b/(.*)$", line)
    return match.group(2) if match else line.removeprefix("diff --git ")


def _untracked(root: Path, path: str) -> FileChange:
    content = (root / path).read_bytes()
    if b"\0" in content[:8000]:
        return FileChange(path=path, status="A", binary=True)
    lines = content.decode("utf-8", errors="replace").splitlines()
    hunk = Hunk(anchor=1, added=[(number, text) for number, text in enumerate(lines, 1)])
    return FileChange(
        path=path,
        status="A",
        additions=len(lines),
        hunks=[hunk] if lines else [],
    )
