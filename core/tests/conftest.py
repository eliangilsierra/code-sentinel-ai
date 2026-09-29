from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

CONTROLLER = """package shop;

class OrderController {
    Order get(long id) {
        return repo.findById(id);
    }
    void delete(long id) {
        repo.deleteById(id);
    }
}
"""


def run_git(cwd: Path, *args: str) -> str:
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
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    run_git(root, "init", "--quiet", "--initial-branch=main")
    (root / "src" / "OrderController.java").write_text(CONTROLLER, encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "--quiet", "-m", "initial")
    return root
