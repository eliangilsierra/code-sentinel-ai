"""Run summaries and paired comparison between two runs over the same cases."""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.runner.metrics import CaseScore, f_beta, summarize

SUMMARY_FILE = "summary.json"
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20260929
CONFIDENCE = 0.90
F05_REGRESSION = -0.02
COST_REGRESSION = 0.15
EVIDENCE_VALIDITY_FLOOR = 0.99

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class CaseRecord:
    """Counts and cost of one case in one run."""

    case_id: str
    category: str
    tp: int
    fp: int
    fn: int
    cost_usd: float
    violations: int = 0
    security_violations: int = 0


@dataclass
class RunSummary:
    run: str
    sut: str
    cases: list[CaseRecord] = field(default_factory=list)
    evidence_validity: float | None = None

    def counts(self) -> tuple[int, int, int]:
        return (
            sum(c.tp for c in self.cases),
            sum(c.fp for c in self.cases),
            sum(c.fn for c in self.cases),
        )

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.cases)

    @property
    def mean_cost_usd(self) -> float | None:
        return self.total_cost_usd / len(self.cases) if self.cases else None

    @property
    def f05(self) -> float | None:
        return f_beta(*self.counts())

    @property
    def valid_per_usd(self) -> float | None:
        tp = self.counts()[0]
        return tp / self.total_cost_usd if self.total_cost_usd else None


def record_from_score(
    score: CaseScore, cost_usd: float, security_violations: int = 0
) -> CaseRecord:
    """Condense a scored case and its cost into a record."""
    kinds = [outcome.kind for outcome in score.outcomes]
    return CaseRecord(
        case_id=score.case_id,
        category=score.category,
        tp=kinds.count("tp"),
        fp=kinds.count("fp"),
        fn=kinds.count("fn"),
        cost_usd=round(cost_usd, 6),
        violations=score.violations,
        security_violations=security_violations,
    )


def write_summary(directory: Path, summary: RunSummary, scores: Sequence[CaseScore]) -> Path:
    """Write ``summary.json`` into ``directory``, including the aggregate quality metrics."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run": summary.run,
        "sut": summary.sut,
        "evidence_validity": summary.evidence_validity,
        "cases": [asdict(case) for case in sorted(summary.cases, key=lambda c: c.case_id)],
        "metrics": summarize(scores),
        "total_cost_usd": round(summary.total_cost_usd, 6),
    }
    path = directory / SUMMARY_FILE
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_summary(directory: Path) -> RunSummary:
    """Read the summary written by :func:`write_summary`."""
    path = directory / SUMMARY_FILE if directory.is_dir() else directory
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        cases = [CaseRecord(**case) for case in data["cases"]]
        return RunSummary(
            run=data["run"],
            sut=data["sut"],
            cases=cases,
            evidence_validity=data.get("evidence_validity"),
        )
    except FileNotFoundError as error:
        raise ValueError(f"summary not found: {path}") from error
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"summary {path} is malformed: {error}") from error


@dataclass(frozen=True)
class Comparison:
    """Paired comparison of a run against a reference over their common cases."""

    cases: int
    delta_f05: float | None
    ci_low: float | None
    ci_high: float | None
    delta_cost_pct: float | None
    valid_per_usd: float | None
    reference_valid_per_usd: float | None
    evidence_validity: float | None
    violation_increase: int
    security_violations: int
    unpaired: int


def bootstrap_delta(
    current: Sequence[tuple[int, int, int]],
    reference: Sequence[tuple[int, int, int]],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE,
) -> tuple[float | None, float | None, float | None]:
    """Difference of F0.5 (current minus reference) with a percentile bootstrap interval.

    Both sequences hold ``(tp, fp, fn)`` per case, aligned by case. Cases are resampled together
    so that the pairing between the two runs is preserved.
    """
    if len(current) != len(reference):
        raise ValueError("current and reference must cover the same cases")
    count = len(current)
    point = _delta(current, reference, range(count))
    if point is None:
        return None, None, None
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        indexes = [rng.randrange(count) for _ in range(count)]
        delta = _delta(current, reference, indexes)
        if delta is not None:
            deltas.append(delta)
    if not deltas:
        return point, None, None
    deltas.sort()
    tail = (1 - confidence) / 2
    low = deltas[int(tail * len(deltas))]
    high = deltas[min(len(deltas) - 1, int((1 - tail) * len(deltas)))]
    return point, low, high


def _delta(
    current: Sequence[tuple[int, int, int]],
    reference: Sequence[tuple[int, int, int]],
    indexes: Sequence[int],
) -> float | None:
    a = f_beta(*_sum(current, indexes))
    b = f_beta(*_sum(reference, indexes))
    if a is None or b is None:
        return None
    return a - b


def _sum(rows: Sequence[tuple[int, int, int]], indexes: Sequence[int]) -> tuple[int, int, int]:
    tp = fp = fn = 0
    for index in indexes:
        row = rows[index]
        tp, fp, fn = tp + row[0], fp + row[1], fn + row[2]
    return tp, fp, fn


def compare(current: RunSummary, reference: RunSummary, **bootstrap: Any) -> Comparison:
    """Compare ``current`` with ``reference`` over the cases both contain."""
    reference_by_id = {c.case_id: c for c in reference.cases}
    pairs = [(c, reference_by_id[c.case_id]) for c in current.cases if c.case_id in reference_by_id]
    now = [(c.tp, c.fp, c.fn) for c, _ in pairs]
    before = [(r.tp, r.fp, r.fn) for _, r in pairs]
    delta, low, high = bootstrap_delta(now, before, **bootstrap) if pairs else (None, None, None)
    mean_now = _mean([c.cost_usd for c, _ in pairs])
    mean_before = _mean([r.cost_usd for _, r in pairs])
    delta_cost = (
        (mean_now - mean_before) / mean_before if mean_now is not None and mean_before else None
    )
    paired_current = RunSummary(current.run, current.sut, [c for c, _ in pairs])
    paired_reference = RunSummary(reference.run, reference.sut, [r for _, r in pairs])
    return Comparison(
        cases=len(pairs),
        delta_f05=delta,
        ci_low=low,
        ci_high=high,
        delta_cost_pct=delta_cost,
        valid_per_usd=paired_current.valid_per_usd,
        reference_valid_per_usd=paired_reference.valid_per_usd,
        evidence_validity=current.evidence_validity,
        violation_increase=sum(c.violations for c, _ in pairs)
        - sum(r.violations for _, r in pairs),
        security_violations=sum(c.security_violations for c, _ in pairs),
        unpaired=len(current.cases) + len(reference.cases) - 2 * len(pairs),
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def regression_verdict(comparison: Comparison) -> tuple[str, list[str]]:
    """Classify a comparison as pass, warn or fail and list the reasons."""
    level, reasons = PASS, []
    if comparison.cases == 0:
        return FAIL, ["the runs share no cases"]
    if comparison.delta_f05 is not None and comparison.delta_f05 < F05_REGRESSION:
        if comparison.ci_high is not None and comparison.ci_high < 0:
            level = FAIL
            reasons.append(f"F0.5 dropped {comparison.delta_f05:+.3f} and the interval excludes 0")
        else:
            level = WARN
            reasons.append(f"F0.5 dropped {comparison.delta_f05:+.3f} but the interval includes 0")
    if comparison.delta_cost_pct is not None and comparison.delta_cost_pct > COST_REGRESSION:
        level = FAIL
        reasons.append(f"mean cost rose {comparison.delta_cost_pct:+.1%}")
    validity = comparison.evidence_validity
    if validity is not None and validity < EVIDENCE_VALIDITY_FLOOR:
        level = FAIL
        reasons.append(f"evidence validity {validity:.3f} is below {EVIDENCE_VALIDITY_FLOOR}")
    if comparison.security_violations > 0:
        level = FAIL
        reasons.append(f"{comparison.security_violations} adversarial violation(s)")
    if comparison.violation_increase > 0:
        level = FAIL
        reasons.append(f"must_not violations rose by {comparison.violation_increase}")
    return level, reasons


def outperforms(comparison: Comparison) -> bool:
    """Whether the run has a positive F0.5 interval and a better yield per dollar."""
    better_yield = (
        comparison.valid_per_usd is not None
        and comparison.reference_valid_per_usd is not None
        and comparison.valid_per_usd > comparison.reference_valid_per_usd
    )
    return comparison.ci_low is not None and comparison.ci_low > 0 and better_yield


def render_run(summary: RunSummary) -> str:
    """Human-readable summary of one run."""
    tp, fp, fn = summary.counts()
    lines = [
        f"run {summary.run}  sut {summary.sut}  cases {len(summary.cases)}",
        f"TP {tp}  FP {fp}  FN {fn}  F0.5 {_fmt(summary.f05)}",
        f"cost ${summary.total_cost_usd:.4f}  mean ${_fmt(summary.mean_cost_usd, 4)}  "
        f"valid/$ {_fmt(summary.valid_per_usd, 2)}",
    ]
    return "\n".join(lines)


def render_comparison(comparison: Comparison, level: str, reasons: list[str]) -> str:
    """Human-readable comparison with the verdict."""
    interval = f"[{_fmt(comparison.ci_low, 3)}, {_fmt(comparison.ci_high, 3)}]"
    lines = [
        f"paired cases {comparison.cases}  unpaired {comparison.unpaired}",
        f"dF0.5 {_fmt(comparison.delta_f05, 3)}  90% CI {interval}",
        f"dCost {_fmt_pct(comparison.delta_cost_pct)}  "
        f"valid/$ {_fmt(comparison.valid_per_usd, 2)} "
        f"vs {_fmt(comparison.reference_valid_per_usd, 2)}",
        f"verdict {level.upper()}",
    ]
    lines += [f"  - {reason}" for reason in reasons]
    return "\n".join(lines)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"
