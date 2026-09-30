"""Invocation of the Claude Code command line in non-interactive mode."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ClaudeResult:
    returncode: int
    stdout: str
    stderr: str
    data: dict[str, Any] | None
    seconds: float

    @property
    def failed(self) -> bool:
        """Whether the process failed or reported an error result."""
        return self.returncode != 0 or bool(self.data and self.data.get("is_error"))

    @property
    def message(self) -> str:
        """Short description of a failure."""
        if self.data and self.data.get("is_error"):
            return str(self.data.get("result", "error result"))
        return (self.stderr or self.stdout).strip()[:500] or f"exit code {self.returncode}"


def parse_result(stdout: str) -> dict[str, Any] | None:
    """The JSON object printed by ``--output-format json``: the whole output or its last line."""
    candidates = [stdout.strip(), *reversed([line for line in stdout.splitlines() if line.strip()])]
    for text in candidates:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def run_claude(
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> ClaudeResult:
    """Run ``command`` in ``cwd`` and parse its JSON result; a timeout is reported as a failure."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except FileNotFoundError as error:
        return ClaudeResult(127, "", f"command not found: {error}", None, 0.0)
    except subprocess.TimeoutExpired:
        return ClaudeResult(124, "", f"timed out after {timeout} seconds", None, timeout or 0.0)
    seconds = time.monotonic() - started
    return ClaudeResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
        parse_result(completed.stdout),
        seconds,
    )
