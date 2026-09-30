from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from evals.runner.fixtures import make_bundle

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@dataclass(frozen=True)
class Fixture:
    fixtures_dir: Path
    name: str
    base: str
    head: str

    @property
    def bundle(self) -> Path:
        return self.fixtures_dir / self.name / f"{self.name}.bundle"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


@pytest.fixture
def shop_fixture(tmp_path: Path) -> Fixture:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "src").mkdir()
    controller = repo / "src" / "OrderController.java"
    controller.write_text("class OrderController {\n  void get() {}\n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    controller.write_text(
        "class OrderController {\n  void get() {}\n  void list() {}\n  void del() {}\n}\n",
        encoding="utf-8",
    )
    git(repo, "commit", "--quiet", "-am", "head")
    head = git(repo, "rev-parse", "HEAD")
    fixtures_dir = tmp_path / "fixtures"
    make_bundle(repo, fixtures_dir / "shop" / "shop.bundle")
    return Fixture(fixtures_dir, "shop", base, head)
