from __future__ import annotations

import copy
from typing import Any

import pytest

from evals.runner.match import (
    MAX_EXPECTED,
    Finding,
    line_distance,
    match_case,
    normalize_path,
    same_file,
    targets_from_case,
)
from evals.runner.metrics import (
    by_lens,
    by_severity,
    by_stack,
    f_beta,
    score_case,
    summarize,
    summarize_by,
)

CASE: dict[str, Any] = {
    "id": "spring-authz-007",
    "category": "bug",
    "stack": ["java", "spring"],
    "expected": [
        {
            "lens": "security",
            "sev": "important",
            "loc": {"f": "src/OrderController.java", "l": [48, 55]},
            "tol": 3,
        },
        {
            "lens": "correctness",
            "sev": "nit",
            "loc": {"f": "src/OrderService.java", "l": [10, 12]},
        },
    ],
    "optional": [{"loc": {"f": "src/Util.java", "l": [5, 6]}, "claim_hint": "tolerated"}],
    "must_not": [
        {"f": "src/OrderRepository.java", "reason": "parameterised"},
        {"pattern": "style|naming"},
    ],
}


def _finding(path: str, start: int, end: int | None = None, sev: str = "important", **kw: Any):
    return Finding(f=path, start=start, end=end if end is not None else start, sev=sev, **kw)


def test_normalize_path_handles_separators_and_prefix() -> None:
    assert normalize_path(".\\src\\A.java") == "src/A.java"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("src/A.java", "src/A.java", True),
        ("shop/src/A.java", "src/A.java", True),
        ("A.java", "src/A.java", True),
        ("src/BA.java", "A.java", False),
        ("src/A.java", "src/B.java", False),
    ],
)
def test_same_file(left: str, right: str, expected: bool) -> None:
    assert same_file(left, right) is expected


def test_line_distance_is_zero_on_overlap_and_respects_tolerance() -> None:
    target = targets_from_case(CASE)[0][0]
    assert line_distance(_finding("src/OrderController.java", 50), target) == 0
    assert line_distance(_finding("src/OrderController.java", 40, 45), target) == 3
    assert line_distance(_finding("src/OrderController.java", 58), target) == 3
    assert line_distance(_finding("src/OrderController.java", 59), target) is None
    assert line_distance(_finding("src/Other.java", 50), target) is None


def test_perfect_match() -> None:
    findings = [
        _finding("src/OrderController.java", 52),
        _finding("src/OrderService.java", 11, sev="nit"),
    ]
    result = match_case(CASE, findings)
    assert [(p.expected, p.finding) for p in result.pairs] == [(0, 0), (1, 1)]
    assert result.missed == [] and result.false_positives == []


def test_missing_and_extra_findings() -> None:
    findings = [_finding("src/Unrelated.java", 1), _finding("src/OrderController.java", 49)]
    result = match_case(CASE, findings)
    assert [(p.expected, p.finding) for p in result.pairs] == [(0, 1)]
    assert result.missed == [1]
    assert result.false_positives == [0]


def test_severity_error_is_recorded_without_losing_the_match() -> None:
    result = match_case(CASE, [_finding("src/OrderController.java", 52, sev="nit")])
    assert result.pairs[0].severity_match is False


def test_assignment_prefers_the_closest_finding() -> None:
    findings = [_finding("src/OrderController.java", 58), _finding("src/OrderController.java", 50)]
    result = match_case(CASE, findings)
    assert [(p.expected, p.finding, p.distance) for p in result.pairs] == [(0, 1, 0)]
    assert result.duplicates == [0]
    assert result.false_positives == []


def test_assignment_prefers_matching_severity_on_equal_distance() -> None:
    findings = [
        _finding("src/OrderController.java", 52, sev="nit"),
        _finding("src/OrderController.java", 52, sev="important"),
    ]
    result = match_case(CASE, findings)
    assert result.pairs[0].finding == 1


def test_assignment_maximises_matches_over_greedy_choice() -> None:
    case = copy.deepcopy(CASE)
    case["expected"] = [
        {"lens": "a", "sev": "nit", "loc": {"f": "A.java", "l": [10, 10]}, "tol": 5},
        {"lens": "a", "sev": "nit", "loc": {"f": "A.java", "l": [14, 14]}, "tol": 5},
    ]
    findings = [_finding("A.java", 12, sev="nit"), _finding("A.java", 8, sev="nit")]
    result = match_case(case, findings)
    assert len(result.pairs) == 2


def test_finding_at_optional_location_is_not_a_false_positive() -> None:
    result = match_case(CASE, [_finding("src/Util.java", 5)])
    assert result.optional_hits == [(0, 0)]
    assert result.false_positives == []


def test_repeated_finding_is_a_duplicate_not_a_false_positive() -> None:
    findings = [_finding("src/OrderController.java", 52), _finding("src/OrderController.java", 53)]
    result = match_case(CASE, findings)
    assert result.duplicates == [1]
    assert result.false_positives == []


def test_must_not_rules_detect_file_and_pattern_violations() -> None:
    findings = [
        _finding("src/OrderRepository.java", 3),
        _finding("src/Elsewhere.java", 9, claim="Naming of variable is unclear"),
    ]
    result = match_case(CASE, findings)
    assert result.violations == [(0, 0), (1, 1)]


def test_too_many_expected_findings_are_rejected() -> None:
    case = copy.deepcopy(CASE)
    case["expected"] = [
        {"lens": "a", "sev": "nit", "loc": {"f": "A.java", "l": [i, i]}}
        for i in range(1, MAX_EXPECTED + 2)
    ]
    with pytest.raises(ValueError, match="limit"):
        match_case(case, [])


def test_f_beta_matches_hand_computed_values() -> None:
    assert f_beta(2, 1, 1) == pytest.approx(1.25 * 2 / (1.25 * 2 + 0.25 * 1 + 1))
    assert f_beta(3, 1, 0) == pytest.approx(0.9375 / 1.1875)
    assert f_beta(0, 2, 1) == 0.0
    assert f_beta(0, 0, 0) is None


def test_f_beta_equals_the_precision_recall_formula() -> None:
    tp, fp, fn = 6, 2, 3
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    formula = 1.25 * precision * recall / (0.25 * precision + recall)
    assert f_beta(tp, fp, fn) == pytest.approx(formula)


def _scores():
    bug = score_case(
        CASE,
        [
            _finding("src/OrderController.java", 52, sev="nit", lens="security"),
            _finding("src/Stray.java", 1, lens="correctness"),
        ],
    )
    negative_case = {
        "id": "clean-001",
        "category": "negative",
        "stack": ["java"],
        "expected": [],
        "must_not": [],
    }
    clean = score_case(negative_case, [])
    noisy = score_case({**negative_case, "id": "clean-002"}, [_finding("A.java", 1)])
    return [bug, clean, noisy]


def test_case_score_records_outcomes_and_severity_pairs() -> None:
    bug = _scores()[0]
    kinds = sorted(o.kind for o in bug.outcomes)
    assert kinds == ["fn", "fp", "tp"]
    assert bug.severity_pairs == [("important", "nit")]
    assert bug.distances == [0]


def test_summary_sums_counts_across_cases() -> None:
    summary = summarize(_scores())
    assert (summary["tp"], summary["fp"], summary["fn"]) == (1, 2, 1)
    assert summary["precision"] == pytest.approx(1 / 3)
    assert summary["recall"] == pytest.approx(1 / 2)
    assert summary["f05"] == pytest.approx(1.25 / (1.25 + 0.25 + 2))
    assert summary["cases"] == 3
    assert summary["fp_per_case"] == pytest.approx(2 / 3)
    assert summary["negative_fp_rate"] == pytest.approx(1 / 2)
    assert summary["severity_confusion"] == {"important": {"nit": 1}}
    assert summary["mean_location_distance"] == 0
    assert summary["duplicate_rate"] == 0


def test_summary_of_no_cases_has_undefined_ratios() -> None:
    summary = summarize([])
    assert summary["precision"] is None and summary["f05"] is None
    assert summary["negative_fp_rate"] is None


def test_summary_groups_by_lens_severity_and_stack() -> None:
    scores = _scores()
    lenses = summarize_by(scores, by_lens)
    assert lenses["security"]["tp"] == 1
    assert lenses["correctness"]["fp"] == 1 and lenses["correctness"]["fn"] == 1
    assert summarize_by(scores, by_severity)["important"]["fp"] == 2
    stacks = summarize_by(scores, by_stack)
    assert stacks["spring"]["tp"] == 1 and stacks["java"]["tp"] == 1
    assert stacks["java"]["fp"] == 2


def test_duplicate_rate_counts_duplicates_over_published() -> None:
    score = score_case(
        CASE,
        [_finding("src/OrderController.java", 52), _finding("src/OrderController.java", 53)],
    )
    assert summarize([score])["duplicate_rate"] == pytest.approx(0.5)


def test_must_not_violations_are_summed() -> None:
    score = score_case(CASE, [_finding("src/OrderRepository.java", 3)])
    assert summarize([score])["must_not_violations"] == 1
