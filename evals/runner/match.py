"""Deterministic matching of published findings against the expectations of a case."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

DEFAULT_TOLERANCE = 3
MAX_EXPECTED = 16
LOCATION_WEIGHT = 2


@dataclass(frozen=True)
class Finding:
    """A finding published by the system under test."""

    f: str
    start: int
    end: int
    sev: str
    claim: str = ""
    lens: str | None = None


@dataclass(frozen=True)
class Target:
    """A labelled location: an expected finding or an optional (tolerated) one."""

    f: str
    start: int
    end: int
    tol: int
    sev: str | None = None
    lens: str | None = None


@dataclass(frozen=True)
class Pair:
    expected: int
    finding: int
    distance: int
    severity_match: bool


@dataclass
class MatchResult:
    """Outcome of matching one case.

    ``missed`` lists expected indexes without a finding, ``false_positives`` lists finding indexes
    that match nothing, ``optional_hits`` pairs findings with tolerated locations, ``duplicates``
    lists findings repeating an already matched expectation and ``violations`` pairs findings with
    the ``must_not`` rule they break.
    """

    pairs: list[Pair] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    false_positives: list[int] = field(default_factory=list)
    optional_hits: list[tuple[int, int]] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    violations: list[tuple[int, int]] = field(default_factory=list)


def normalize_path(path: str) -> str:
    """Return ``path`` with forward slashes and without a leading ``./``."""
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def same_file(left: str, right: str) -> bool:
    """Whether two paths name the same file, allowing one to be a suffix of the other."""
    a, b = normalize_path(left), normalize_path(right)
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def line_distance(finding: Finding, target: Target) -> int | None:
    """Distance in lines between a finding and a target, or ``None`` beyond the tolerance.

    A finding whose range overlaps the target range exactly has distance zero.
    """
    if not same_file(finding.f, target.f):
        return None
    gap = max(0, target.start - finding.end, finding.start - target.end)
    return gap if gap <= target.tol else None


def targets_from_case(case: dict[str, Any]) -> tuple[list[Target], list[Target]]:
    """Return the expected and optional targets of a case."""
    expected = [
        Target(
            f=item["loc"]["f"],
            start=item["loc"]["l"][0],
            end=item["loc"]["l"][1],
            tol=item.get("tol", DEFAULT_TOLERANCE),
            sev=item["sev"],
            lens=item["lens"],
        )
        for item in case["expected"]
    ]
    optional = [
        Target(
            f=item["loc"]["f"],
            start=item["loc"]["l"][0],
            end=item["loc"]["l"][1],
            tol=DEFAULT_TOLERANCE,
        )
        for item in case.get("optional", [])
    ]
    return expected, optional


def match_case(case: dict[str, Any], findings: list[Finding]) -> MatchResult:
    """Match ``findings`` against the expectations of ``case`` one to one."""
    expected, optional = targets_from_case(case)
    if len(expected) > MAX_EXPECTED:
        raise ValueError(f"case has {len(expected)} expected findings; the limit is {MAX_EXPECTED}")
    result = MatchResult()
    costs = [[_cost(finding, target) for target in expected] for finding in findings]
    assigned = _assign(costs)
    matched_findings = {i for i, _ in assigned}
    matched_expected = {e for _, e in assigned}
    for finding_index, expected_index in sorted(assigned, key=lambda pair: pair[1]):
        finding, target = findings[finding_index], expected[expected_index]
        distance = line_distance(finding, target)
        assert distance is not None
        result.pairs.append(
            Pair(expected_index, finding_index, distance, finding.sev == target.sev)
        )
    result.missed = [i for i in range(len(expected)) if i not in matched_expected]
    for index, finding in enumerate(findings):
        if index in matched_findings:
            continue
        optional_index = _first_hit(finding, optional)
        if optional_index is not None:
            result.optional_hits.append((index, optional_index))
        elif any(line_distance(finding, expected[e]) is not None for e in sorted(matched_expected)):
            result.duplicates.append(index)
        else:
            result.false_positives.append(index)
    result.violations = _violations(case["must_not"], findings)
    return result


def _cost(finding: Finding, target: Target) -> int | None:
    distance = line_distance(finding, target)
    if distance is None:
        return None
    return distance * LOCATION_WEIGHT + (0 if finding.sev == target.sev else 1)


def _first_hit(finding: Finding, targets: list[Target]) -> int | None:
    for index, target in enumerate(targets):
        if line_distance(finding, target) is not None:
            return index
    return None


def _violations(rules: list[dict[str, Any]], findings: list[Finding]) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    for rule_index, rule in enumerate(rules):
        pattern = re.compile(rule["pattern"], re.IGNORECASE) if "pattern" in rule else None
        for finding_index, finding in enumerate(findings):
            if pattern is not None and pattern.search(finding.claim):
                hits.append((finding_index, rule_index))
            elif pattern is None and same_file(finding.f, rule["f"]):
                hits.append((finding_index, rule_index))
    return hits


def _assign(costs: list[list[int | None]]) -> list[tuple[int, int]]:
    """Choose a one-to-one assignment maximising matches, then minimising total cost."""
    finding_count = len(costs)

    @cache
    def best(index: int, used: int) -> tuple[int, int, tuple[tuple[int, int], ...]]:
        if index == finding_count:
            return (0, 0, ())
        options = [best(index + 1, used)]
        for expected_index, cost in enumerate(costs[index]):
            if cost is None or used & (1 << expected_index):
                continue
            rest = best(index + 1, used | (1 << expected_index))
            options.append((rest[0] - 1, rest[1] + cost, ((index, expected_index), *rest[2])))
        return min(options)

    return list(best(0, 0)[2])
