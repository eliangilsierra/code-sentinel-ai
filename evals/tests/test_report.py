from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner.__main__ import main
from evals.runner.metrics import f_beta
from evals.runner.report import (
    FAIL,
    PASS,
    WARN,
    CaseRecord,
    RunSummary,
    bootstrap_delta,
    compare,
    load_summary,
    outperforms,
    regression_verdict,
    render_comparison,
    render_run,
    write_summary,
)


def _run(name: str, rows: list[tuple[int, int, int]], cost: float = 0.1, **extra) -> RunSummary:
    cases = [
        CaseRecord(f"case-{i:03d}", "bug", tp, fp, fn, cost, **extra)
        for i, (tp, fp, fn) in enumerate(rows)
    ]
    return RunSummary(run=name, sut="review-squad", cases=cases)


def test_point_estimate_is_the_difference_of_summed_f05() -> None:
    current = [(2, 1, 0), (1, 0, 1)]
    reference = [(1, 0, 1), (1, 1, 0)]
    delta, _, _ = bootstrap_delta(current, reference, samples=50)
    assert delta == pytest.approx(f_beta(3, 1, 1) - f_beta(2, 1, 1))


def test_bootstrap_is_reproducible_for_a_fixed_seed() -> None:
    current = [(1, 1, 0)] * 6 + [(1, 0, 0)] * 14
    reference = [(1, 0, 0)] * 20
    first = bootstrap_delta(current, reference, samples=500, seed=7)
    second = bootstrap_delta(current, reference, samples=500, seed=7)
    assert first == second
    assert first[1] < first[0] < first[2]


def test_misaligned_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="same cases"):
        bootstrap_delta([(1, 0, 0)], [], samples=10)


def test_undefined_f05_yields_no_interval() -> None:
    assert bootstrap_delta([(0, 0, 0)], [(0, 0, 0)], samples=10) == (None, None, None)


def test_identical_runs_pass_with_a_zero_delta() -> None:
    rows = [(1, 0, 0)] * 30
    comparison = compare(_run("a", rows), _run("b", rows))
    assert comparison.delta_f05 == 0
    assert regression_verdict(comparison) == (PASS, [])


def test_large_consistent_regression_fails() -> None:
    reference = _run("ref", [(1, 0, 0)] * 40)
    current = _run("cur", [(1, 1, 0)] * 6 + [(1, 0, 0)] * 34)
    comparison = compare(current, reference)
    level, reasons = regression_verdict(comparison)
    assert level == FAIL
    assert comparison.delta_f05 < -0.02 and comparison.ci_high < 0
    assert "interval excludes 0" in reasons[0]


def test_noise_within_the_threshold_passes() -> None:
    reference = _run("ref", [(1, 0, 0)] * 40)
    current = _run("cur", [(1, 0, 0)] * 39 + [(1, 1, 0)])
    comparison = compare(current, reference)
    assert comparison.delta_f05 > -0.02
    assert regression_verdict(comparison)[0] == PASS


def test_regression_with_a_wide_interval_only_warns() -> None:
    reference = _run("ref", [(1, 0, 0)] * 5)
    current = _run("cur", [(1, 1, 0)] + [(1, 0, 0)] * 4)
    level, reasons = regression_verdict(compare(current, reference))
    assert level == WARN
    assert "includes 0" in reasons[0]


def test_cost_increase_above_fifteen_percent_fails() -> None:
    rows = [(1, 0, 0)] * 10
    level, reasons = regression_verdict(compare(_run("c", rows, 0.12), _run("r", rows, 0.10)))
    assert level == FAIL and "cost rose" in reasons[0]


def test_cost_increase_within_fifteen_percent_passes() -> None:
    rows = [(1, 0, 0)] * 10
    assert regression_verdict(compare(_run("c", rows, 0.114), _run("r", rows, 0.10)))[0] == PASS


def test_low_evidence_validity_fails() -> None:
    rows = [(1, 0, 0)] * 10
    current = _run("c", rows)
    current.evidence_validity = 0.97
    assert regression_verdict(compare(current, _run("r", rows)))[0] == FAIL


def test_security_and_must_not_violations_fail() -> None:
    rows = [(1, 0, 0)] * 10
    secure = compare(_run("c", rows, security_violations=1), _run("r", rows))
    assert regression_verdict(secure)[0] == FAIL
    must_not = compare(_run("c", rows, violations=1), _run("r", rows))
    assert regression_verdict(must_not)[0] == FAIL


def test_runs_without_common_cases_fail() -> None:
    other = RunSummary("o", "x", [CaseRecord("other", "bug", 1, 0, 0, 0.1)])
    comparison = compare(_run("c", [(1, 0, 0)]), other)
    assert regression_verdict(comparison) == (FAIL, ["the runs share no cases"])
    assert comparison.unpaired == 2


def test_only_common_cases_are_compared() -> None:
    reference = _run("r", [(1, 0, 0)] * 3)
    current = _run("c", [(1, 0, 0)] * 5)
    comparison = compare(current, reference)
    assert comparison.cases == 3 and comparison.unpaired == 2


def test_outperforms_needs_a_positive_interval_and_a_better_yield() -> None:
    reference = _run("ref", [(1, 1, 0)] * 30, cost=0.2)
    better = _run("cur", [(1, 0, 0)] * 30, cost=0.1)
    assert outperforms(compare(better, reference))
    same = _run("cur", [(1, 1, 0)] * 30, cost=0.2)
    assert not outperforms(compare(same, reference))
    costly = _run("cur", [(1, 0, 0)] * 30, cost=0.4)
    assert not outperforms(compare(costly, reference))


def test_summary_round_trips_through_disk(tmp_path: Path) -> None:
    summary = _run("run-1", [(1, 0, 0), (0, 1, 1)], cost=0.25)
    summary.evidence_validity = 1.0
    write_summary(tmp_path / "run-1", summary, [])
    loaded = load_summary(tmp_path / "run-1")
    assert loaded.cases == sorted(summary.cases, key=lambda c: c.case_id)
    assert loaded.evidence_validity == 1.0 and loaded.sut == "review-squad"


def test_loading_a_missing_or_malformed_summary_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_summary(tmp_path / "none")
    (tmp_path / "summary.json").write_text('{"run": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        load_summary(tmp_path)


def test_rendering_shows_counts_and_verdict() -> None:
    reference, current = _run("r", [(1, 0, 0)] * 10), _run("c", [(1, 0, 0)] * 10)
    assert "TP 10  FP 0  FN 0  F0.5 1.000" in render_run(current)
    comparison = compare(current, reference)
    text = render_comparison(comparison, *regression_verdict(comparison))
    assert "verdict PASS" in text and "paired cases 10" in text


def _write(tmp_path: Path, name: str, summary: RunSummary) -> Path:
    write_summary(tmp_path / name, summary, [])
    return tmp_path / name


def test_cli_prints_a_single_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _write(tmp_path, "a", _run("a", [(1, 0, 0)] * 3))
    assert main(["report", str(run)]) == 0
    assert "cases 3" in capsys.readouterr().out


def test_cli_exits_one_on_a_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reference = _write(tmp_path, "ref", _run("ref", [(1, 0, 0)] * 40))
    current = _write(tmp_path, "cur", _run("cur", [(1, 1, 0)] * 6 + [(1, 0, 0)] * 34))
    assert main(["report", str(current), "--compare", str(reference)]) == 1
    assert "verdict FAIL" in capsys.readouterr().out


def test_cli_exits_zero_on_noise(tmp_path: Path) -> None:
    reference = _write(tmp_path, "ref", _run("ref", [(1, 0, 0)] * 40))
    current = _write(tmp_path, "cur", _run("cur", [(1, 0, 0)] * 39 + [(1, 1, 0)]))
    assert main(["report", str(current), "--compare", str(reference)]) == 0


def test_cli_exits_two_when_a_summary_is_missing(tmp_path: Path) -> None:
    assert main(["report", str(tmp_path / "none")]) == 2
