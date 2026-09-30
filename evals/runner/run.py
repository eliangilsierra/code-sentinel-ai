"""Execution of an evaluation suite: materialize each case, run a system, score and summarize."""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from review_ctx.schemas import validation_errors

from evals.runner.budget import Budget
from evals.runner.cost import DEFAULT_PRICING, Pricing, Usage, cache_read_ratio, equivalent_cost
from evals.runner.fixtures import bundle_path, materialized
from evals.runner.match import Finding
from evals.runner.metrics import CaseScore, score_case
from evals.runner.report import RunSummary, record_from_score, write_summary
from evals.runner.sut import Outcome

OUTCOME_FILE = "outcome.json"
RAW_FILE = "claude.json"
ALL_SUITES = ("all", "full")


class RunError(Exception):
    """Raised when a suite cannot be resolved or a case is invalid."""


class Sut(Protocol):
    """A system under test that reviews a materialized repository."""

    name: str

    def run(self, repo: Path, case: dict[str, Any]) -> Outcome: ...


@dataclass
class RunOptions:
    suite: str
    run_id: str
    cases_dir: Path
    fixtures_dir: Path
    suites_dir: Path
    reports_dir: Path
    force: bool = False
    pricing: Path = DEFAULT_PRICING


@dataclass
class CaseResult:
    case_id: str
    score: CaseScore | None
    cost_usd: float
    extra_cost_usd: float
    outcome: Outcome
    reused: bool = False


@dataclass
class SuiteResult:
    summary: RunSummary
    scores: list[CaseScore]
    results: list[CaseResult] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    directory: Path | None = None

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd + r.extra_cost_usd for r in self.results if not r.reused)


def load_case(path: Path) -> dict[str, Any]:
    """Read a case file and check it against the case schema."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RunError(f"{path}: cannot read case: {error}") from error
    problems = validation_errors("case", data)
    if problems:
        raise RunError(f"{path}: invalid case: {'; '.join(problems)}")
    return data


def resolve_cases(suite: str, cases_dir: Path, suites_dir: Path) -> list[Path]:
    """Case files of ``suite``: a suite file, ``all``, or a glob over case ids."""
    every = sorted(cases_dir.rglob("*.yaml")) if cases_dir.is_dir() else []
    listing = suites_dir / f"{suite}.txt"
    if listing.is_file():
        wanted = [
            line.strip()
            for line in listing.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        by_id = {path.stem: path for path in every}
        missing = [case_id for case_id in wanted if case_id not in by_id]
        if missing:
            raise RunError(f"suite {suite!r} lists unknown cases: {', '.join(missing)}")
        return [by_id[case_id] for case_id in wanted]
    if suite in ALL_SUITES:
        return every
    matched = [path for path in every if fnmatch.fnmatch(path.stem, suite)]
    if not matched:
        raise RunError(f"suite {suite!r} matches no case")
    return matched


def _usage_from_json(items: list[dict[str, Any]]) -> list[Usage]:
    return [Usage(**item) for item in items]


def _write_outcome(directory: Path, outcome: Outcome, score: CaseScore | None, cost: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "findings": [asdict(f) for f in outcome.findings],
        "suppressed": outcome.suppressed,
        "usages": [asdict(u) for u in outcome.usages],
        "extra_usages": [asdict(u) for u in outcome.extra_usages],
        "seconds": round(outcome.seconds, 3),
        "error": outcome.error,
        "cost_usd": round(cost, 6),
        "cache_read_ratio": cache_read_ratio(outcome.usages),
        "scored": score is not None,
    }
    (directory / OUTCOME_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if outcome.raw is not None:
        (directory / RAW_FILE).write_text(
            json.dumps(outcome.raw, indent=2) + "\n", encoding="utf-8"
        )


def _load_outcome(directory: Path) -> Outcome | None:
    path = directory / OUTCOME_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("error"):
        return None
    return Outcome(
        findings=[Finding(**f) for f in data["findings"]],
        suppressed=data["suppressed"],
        usages=_usage_from_json(data["usages"]),
        extra_usages=_usage_from_json(data["extra_usages"]),
        seconds=data["seconds"],
    )


def run_suite(
    options: RunOptions, sut: Sut, on_case: Callable[[str], None] | None = None
) -> SuiteResult:
    """Run ``options.suite`` with ``sut``; cases with a stored, error-free outcome are reused."""
    paths = resolve_cases(options.suite, options.cases_dir, options.suites_dir)
    pricing = Pricing.load(options.pricing)
    base = options.reports_dir / options.run_id / sut.name
    result = SuiteResult(summary=RunSummary(options.run_id, sut.name), scores=[], directory=base)
    for path in paths:
        case = load_case(path)
        directory = base / case["id"]
        stored = None if options.force else _load_outcome(directory)
        reused = stored is not None
        if on_case:
            on_case(case["id"])
        outcome = stored or _run_case(options, sut, case)
        cost = equivalent_cost(outcome.usages, pricing)
        extra = equivalent_cost(outcome.extra_usages, pricing)
        score = None if outcome.error else score_case(case, outcome.findings)
        if not reused:
            _write_outcome(directory, outcome, score, cost)
        result.results.append(CaseResult(case["id"], score, cost, extra, outcome, reused))
        if score is None:
            result.errors.append((case["id"], outcome.error or "unknown error"))
            continue
        result.scores.append(score)
        result.summary.cases.append(record_from_score(score, cost))
    write_summary(base, result.summary, result.scores)
    return result


def _run_case(options: RunOptions, sut: Sut, case: dict[str, Any]) -> Outcome:
    bundle = bundle_path(options.fixtures_dir, case["fixture"])
    with materialized(bundle, case["base"], case["head"]) as repo:
        return sut.run(repo, case)


def pending_cases(options: RunOptions, sut_name: str) -> int:
    """Number of cases that would call the system: those without a stored, error-free outcome."""
    base = options.reports_dir / options.run_id / sut_name
    paths = resolve_cases(options.suite, options.cases_dir, options.suites_dir)
    if options.force:
        return len(paths)
    return sum(1 for path in paths if _load_outcome(base / path.stem) is None)


def charge(budget: Budget, run_id: str, result: SuiteResult) -> float:
    """Record the cost of the calls made by ``result`` in ``budget``."""
    total = result.total_cost_usd
    if total > 0:
        budget.record(run_id, total)
    return total
