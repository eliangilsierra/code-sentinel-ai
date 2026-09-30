"""Systems under test: review-squad and the reference reviewer, driven through ``claude -p``."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.runner.claude_cli import ClaudeResult, run_claude
from evals.runner.cost import Usage, parse_usage
from evals.runner.match import Finding

DEFAULT_TIMEOUT = 1800.0
EXTRACTION_MODEL = "haiku"
EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["f", "l", "claim", "sev", "category"],
                "additionalProperties": False,
                "properties": {
                    "f": {"type": "string"},
                    "l": {"type": "integer", "minimum": 1},
                    "claim": {"type": "string"},
                    "sev": {"enum": ["important", "nit", "pre_existing", "question"]},
                    "category": {"enum": ["defect", "cleanup"]},
                },
            },
        }
    },
}
EXTRACTION_PROMPT = (
    "The text on standard input is a code review report. Extract every finding it makes as "
    "JSON. For each one give the file path, the first line number it refers to, a one-sentence "
    "claim, its severity (important, nit or pre_existing) and its category: 'defect' when it "
    "reports incorrect or unsafe behaviour, 'cleanup' when it only suggests reuse, "
    "simplification, efficiency or style. Do not invent findings."
)


@dataclass
class SutConfig:
    """How to launch Claude Code for an evaluation run."""

    claude: list[str] = field(default_factory=lambda: ["claude"])
    plugin_dir: Path | None = None
    config_dir: Path | None = None
    effort: str = "medium"
    permission_mode: str = "dontAsk"
    cap_usd: float = 0.5
    timeout: float = DEFAULT_TIMEOUT
    core: list[str] = field(default_factory=lambda: [sys.executable, "-m", "review_ctx.cli"])
    env: dict[str, str] = field(default_factory=dict)

    def environment(self) -> dict[str, str]:
        env = dict(self.env)
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return env


@dataclass
class Outcome:
    """What a system produced for one case."""

    findings: list[Finding] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None
    extra_usages: list[Usage] = field(default_factory=list)


def finding_from_entry(entry: dict[str, Any]) -> Finding:
    """Convert a published entry of ``review-ctx report --format json`` into a finding."""
    start, end = entry["loc"]["l"]
    return Finding(
        f=entry["loc"]["f"],
        start=start,
        end=end,
        sev=entry["sev"],
        claim=entry.get("claim", ""),
        lens=entry.get("lens"),
    )


class ReviewSquadSut:
    """The review-squad plugin, run through its review skill."""

    name = "review-squad"

    def __init__(self, config: SutConfig) -> None:
        self.config = config

    def command(self) -> list[str]:
        config = self.config
        command = [
            *config.claude,
            "-p",
            "/review-squad:review",
            "--output-format",
            "json",
            "--permission-mode",
            config.permission_mode,
            "--max-budget-usd",
            f"{config.cap_usd:.2f}",
            "--effort",
            config.effort,
        ]
        if config.plugin_dir is not None:
            command += ["--plugin-dir", str(config.plugin_dir)]
        return command

    def run(self, repo: Path, case: dict[str, Any]) -> Outcome:
        result = run_claude(
            self.command(), repo, self.config.environment(), timeout=self.config.timeout
        )
        outcome = Outcome(seconds=result.seconds, raw=result.data)
        if result.data:
            outcome.usages = parse_usage(result.data)
        if result.failed:
            outcome.error = result.message
        self._collect(repo, outcome)
        return outcome

    def _collect(self, repo: Path, outcome: Outcome) -> None:
        run = latest_run(repo)
        if run is None:
            outcome.error = outcome.error or "the review did not create a run"
            return
        report = run_claude(
            [*self.config.core, "report", "--run", run, "--format", "json", "--repo", str(repo)],
            repo,
            timeout=120,
        )
        if report.data is None:
            outcome.error = outcome.error or f"report failed: {report.message}"
            return
        outcome.findings = [finding_from_entry(e) for e in report.data.get("published", [])]
        outcome.suppressed = report.data.get("suppressed", [])


def latest_run(repo: Path) -> str | None:
    """Id of the most recently modified run of ``repo``, or ``None`` if there is none."""
    runs = repo / ".git" / "review-squad" / "runs"
    if not runs.is_dir():
        return None
    directories = [path for path in runs.iterdir() if path.is_dir()]
    if not directories:
        return None
    return max(directories, key=lambda path: path.stat().st_mtime).name


class CodeReviewSut:
    """The bundled ``/code-review`` command, whose text report is converted into findings."""

    name = "code-review-medium"

    def __init__(self, config: SutConfig, level: str = "medium") -> None:
        self.config = config
        self.level = level

    def run(self, repo: Path, case: dict[str, Any]) -> Outcome:
        config = self.config
        review = run_claude(
            [
                *config.claude,
                "-p",
                f"/code-review {self.level}",
                "--output-format",
                "json",
                "--permission-mode",
                config.permission_mode,
                "--max-budget-usd",
                f"{config.cap_usd:.2f}",
            ],
            repo,
            config.environment(),
            timeout=config.timeout,
        )
        outcome = Outcome(seconds=review.seconds, raw=review.data)
        if review.data:
            outcome.usages = parse_usage(review.data)
        if review.failed:
            outcome.error = review.message
            return outcome
        report = str(review.data.get("result", "")) if review.data else ""
        self._extract(report, repo, outcome)
        return outcome

    def _extract(self, report: str, repo: Path, outcome: Outcome) -> None:
        config = self.config
        extraction = run_claude(
            [
                *config.claude,
                "-p",
                EXTRACTION_PROMPT,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(EXTRACTION_SCHEMA),
                "--model",
                EXTRACTION_MODEL,
                "--tools",
                "",
                "--no-session-persistence",
            ],
            repo,
            config.environment(),
            stdin=report,
            timeout=config.timeout,
        )
        if extraction.data:
            outcome.extra_usages = parse_usage(extraction.data)
        structured = _structured(extraction)
        if structured is None:
            outcome.error = f"could not extract findings: {extraction.message}"
            return
        outcome.findings = [
            Finding(
                f=item["f"],
                start=item["l"],
                end=item["l"],
                sev=item["sev"],
                claim=item["claim"],
            )
            for item in structured.get("findings", [])
            if item["category"] == "defect"
        ]


def _structured(result: ClaudeResult) -> dict[str, Any] | None:
    if result.failed or not result.data:
        return None
    value = result.data.get("structured_output")
    return value if isinstance(value, dict) else None
