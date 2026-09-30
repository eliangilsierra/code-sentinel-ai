"""Location of the repository and of the per-run state stored inside its git directory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RUN_ID = re.compile(r"^[a-z0-9]+$")
STATE_DIR = "review-squad"


class RepoError(Exception):
    """Raised when the repository or a run cannot be located."""


def repo_root(start: Path | None = None) -> Path:
    """Return the top-level directory of the git repository containing ``start``."""
    output = _git(start or Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(output.strip())


def git_dir(root: Path) -> Path:
    """Return the absolute git directory of the repository at ``root``."""
    output = _git(root, "rev-parse", "--absolute-git-dir")
    return Path(output.strip())


def runs_dir(root: Path) -> Path:
    """Directory holding every run of the repository at ``root``."""
    return git_dir(root) / STATE_DIR / "runs"


def run_dir(root: Path, run: str, create: bool = False) -> Path:
    """Directory of run ``run``; created on request, otherwise it must exist."""
    if not RUN_ID.match(run):
        raise RepoError(f"invalid run id {run!r}; use lowercase letters and digits")
    path = runs_dir(root) / run
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise RepoError(f"run {run!r} not found in {path.parent}")
    return path


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
        )
    except FileNotFoundError as error:
        raise RepoError("git is not installed or not on PATH") from error
    if result.returncode != 0:
        raise RepoError(f"not a git repository: {cwd}")
    return result.stdout
