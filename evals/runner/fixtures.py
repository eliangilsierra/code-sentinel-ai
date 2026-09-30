"""Fixture repositories stored as git bundles and their materialization for evaluation runs."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

MARKER = "review-squad-fixture"
REVIEW_BRANCH = "review"
BASE_REF = "refs/remotes/origin/review-base"

LineCounter = Callable[[str], int | None]


class FixtureError(Exception):
    """Raised when a fixture bundle or one of its commits cannot be used."""


def bundle_path(fixtures_dir: Path, fixture: str) -> Path:
    """Return the location of the bundle of ``fixture`` inside ``fixtures_dir``."""
    return fixtures_dir / fixture / f"{fixture}.bundle"


def make_bundle(repo: Path, bundle: Path) -> None:
    """Write every ref of the repository at ``repo`` into ``bundle``."""
    bundle.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "bundle", "create", str(bundle.resolve()), "--all")


def materialize(bundle: Path, base: str, head: str, dest: Path) -> Path:
    """Clone ``bundle`` into ``dest`` with ``head`` checked out and ``base`` as its upstream.

    Calling it again with the same arguments restores the working tree to ``head``. A non-empty
    ``dest`` that was not produced by this function for the same bundle and commits is rejected.
    """
    marker_value = f"{bundle.stem}:{base}:{head}"
    if dest.exists() and any(dest.iterdir()):
        marker = dest / ".git" / MARKER
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != marker_value:
            raise FixtureError(f"{dest} exists and is not a materialization of {marker_value}")
    else:
        if not bundle.is_file():
            raise FixtureError(f"fixture bundle not found: {bundle}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(dest.parent, "clone", "--quiet", str(bundle), str(dest))
    _checkout(dest, base, head)
    (dest / ".git" / MARKER).write_text(marker_value, encoding="utf-8")
    return dest


@contextmanager
def materialized(bundle: Path, base: str, head: str) -> Iterator[Path]:
    """Materialize into a temporary directory that is removed on exit."""
    root = Path(tempfile.mkdtemp(prefix="review-squad-"))
    try:
        yield materialize(bundle, base, head, root / "repo")
    finally:
        remove_tree(root)


def line_counter(repo: Path, ref: str) -> LineCounter:
    """Return a function giving the line count of a path at ``ref``, or ``None`` if absent."""

    def count(path: str) -> int | None:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, check=False
        )
        return len(result.stdout.splitlines()) if result.returncode == 0 else None

    return count


class FixtureIndex:
    """Lazily opened read-only clones of fixtures, used to check case locations."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir
        self._root: Path | None = None
        self._clones: dict[str, Path] = {}

    def __enter__(self) -> FixtureIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._root is not None:
            remove_tree(self._root)
            self._root = None
            self._clones.clear()

    def line_counter_for(self, case: dict[str, Any]) -> LineCounter:
        """Return the line counter for the head commit of ``case``."""
        clone = self._clone(case["fixture"])
        for ref in (case["base"], case["head"]):
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                cwd=clone,
                capture_output=True,
                check=False,
            )
            if probe.returncode != 0:
                raise FixtureError(f"commit {ref} not found in fixture '{case['fixture']}'")
        return line_counter(clone, case["head"])

    def _clone(self, fixture: str) -> Path:
        if fixture not in self._clones:
            bundle = bundle_path(self._fixtures_dir, fixture)
            if not bundle.is_file():
                raise FixtureError(f"fixture bundle not found: {bundle}")
            if self._root is None:
                self._root = Path(tempfile.mkdtemp(prefix="review-squad-index-"))
            target = self._root / f"{fixture}.git"
            _git(self._root, "clone", "--bare", "--quiet", str(bundle), str(target))
            self._clones[fixture] = target
        return self._clones[fixture]


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only files such as git object packs."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        for root, dirs, files in os.walk(path):
            for name in [*dirs, *files]:
                os.chmod(Path(root) / name, stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(path)


def _checkout(repo: Path, base: str, head: str) -> None:
    full_base = _resolve(repo, base)
    full_head = _resolve(repo, head)
    _git(repo, "update-ref", BASE_REF, full_base)
    _git(repo, "checkout", "--quiet", "--force", "-B", REVIEW_BRANCH, full_head)
    _git(repo, "branch", "--quiet", f"--set-upstream-to={BASE_REF.removeprefix('refs/remotes/')}")
    _git(repo, "clean", "--quiet", "-fd")


def _resolve(repo: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise FixtureError(f"commit {ref} not found in the fixture")
    return result.stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise FixtureError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
