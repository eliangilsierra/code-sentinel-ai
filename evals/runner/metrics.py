"""Quality metrics computed from the matching of findings against cases."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from evals.runner.match import Finding, match_case, targets_from_case

UNKNOWN_LENS = "unknown"


@dataclass(frozen=True)
class Outcome:
    """One true positive, false positive or false negative, with the labels used to group it."""

    kind: str
    lens: str
    sev: str
    stack: tuple[str, ...]


@dataclass
class CaseScore:
    case_id: str
    category: str
    outcomes: list[Outcome] = field(default_factory=list)
    distances: list[int] = field(default_factory=list)
    severity_pairs: list[tuple[str, str]] = field(default_factory=list)
    published: int = 0
    duplicates: int = 0
    violations: int = 0

    @property
    def false_positives(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.kind == "fp")


def score_case(case: dict[str, Any], findings: list[Finding]) -> CaseScore:
    """Match ``findings`` against ``case`` and record every outcome."""
    result = match_case(case, findings)
    expected, _ = targets_from_case(case)
    stack = tuple(case["stack"])
    score = CaseScore(
        case_id=case["id"],
        category=case["category"],
        published=len(findings),
        duplicates=len(result.duplicates),
        violations=len(result.violations),
    )
    for pair in result.pairs:
        target = expected[pair.expected]
        score.outcomes.append(Outcome("tp", target.lens or UNKNOWN_LENS, target.sev or "", stack))
        score.distances.append(pair.distance)
        score.severity_pairs.append((target.sev or "", findings[pair.finding].sev))
    for index in result.missed:
        target = expected[index]
        score.outcomes.append(Outcome("fn", target.lens or UNKNOWN_LENS, target.sev or "", stack))
    for index in result.false_positives:
        finding = findings[index]
        score.outcomes.append(Outcome("fp", finding.lens or UNKNOWN_LENS, finding.sev, stack))
    return score


def f_beta(tp: int, fp: int, fn: int, beta: float = 0.5) -> float | None:
    """F-beta from counts; ``None`` when there is nothing to score."""
    weight = beta * beta
    denominator = (1 + weight) * tp + weight * fn + fp
    if denominator == 0:
        return None
    return (1 + weight) * tp / denominator


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(scores: Iterable[CaseScore]) -> dict[str, Any]:
    """Aggregate scores of many cases by summing their counts."""
    scores = list(scores)
    return _summary(scores, [outcome for score in scores for outcome in score.outcomes])


def summarize_by(
    scores: Iterable[CaseScore], key: Callable[[Outcome], Iterable[str]]
) -> dict[str, dict[str, Any]]:
    """Summaries of the outcomes grouped by ``key``; one outcome may belong to several groups."""
    scores = list(scores)
    groups: dict[str, list[Outcome]] = defaultdict(list)
    for score in scores:
        for outcome in score.outcomes:
            for group in key(outcome):
                groups[group].append(outcome)
    return {name: _outcome_summary(outcomes) for name, outcomes in sorted(groups.items())}


def by_lens(outcome: Outcome) -> Iterable[str]:
    return [outcome.lens]


def by_severity(outcome: Outcome) -> Iterable[str]:
    return [outcome.sev]


def by_stack(outcome: Outcome) -> Iterable[str]:
    return outcome.stack


def _outcome_summary(outcomes: list[Outcome]) -> dict[str, Any]:
    kinds = Counter(outcome.kind for outcome in outcomes)
    tp, fp, fn = kinds["tp"], kinds["fp"], kinds["fn"]
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f05": f_beta(tp, fp, fn),
    }


def _summary(scores: list[CaseScore], outcomes: list[Outcome]) -> dict[str, Any]:
    negatives = [score for score in scores if score.category == "negative"]
    distances = [distance for score in scores for distance in score.distances]
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for score in scores:
        for expected, found in score.severity_pairs:
            confusion[expected][found] += 1
    published = sum(score.published for score in scores)
    summary = _outcome_summary(outcomes)
    summary.update(
        {
            "cases": len(scores),
            "fp_per_case": _ratio(summary["fp"], len(scores)),
            "negative_fp_rate": _ratio(
                sum(1 for s in negatives if s.false_positives), len(negatives)
            ),
            "duplicate_rate": _ratio(sum(score.duplicates for score in scores), published),
            "mean_location_distance": _ratio(sum(distances), len(distances)),
            "severity_confusion": {k: dict(v) for k, v in sorted(confusion.items())},
            "must_not_violations": sum(score.violations for score in scores),
        }
    )
    return summary
