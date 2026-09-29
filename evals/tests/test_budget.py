from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.runner.__main__ import main
from evals.runner.budget import Budget, BudgetError


def test_remaining_budget_decreases_with_each_recorded_run() -> None:
    budget = Budget(limit_usd=100)
    budget.record("run-1", 12.5)
    budget.record("run-2", 7.5)
    assert budget.spent_usd == 20
    assert budget.remaining_usd == 80


def test_negative_cost_is_rejected() -> None:
    with pytest.raises(BudgetError, match="negative"):
        Budget(limit_usd=10).record("run-1", -1)


def test_projection_includes_the_margin() -> None:
    assert Budget(limit_usd=100).projection(20, 0.5, margin=0.1) == pytest.approx(11.0)


def test_affordable_projection_is_returned() -> None:
    budget = Budget(limit_usd=100)
    budget.record("run-1", 90)
    assert budget.ensure_affordable(10, 0.5) == pytest.approx(5.0)


def test_projection_above_the_remaining_budget_is_refused() -> None:
    budget = Budget(limit_usd=100)
    budget.record("run-1", 96)
    with pytest.raises(BudgetError, match=r"projected \$5\.00 exceeds the remaining \$4\.00"):
        budget.ensure_affordable(10, 0.5)


def test_projection_equal_to_the_remaining_budget_is_allowed() -> None:
    budget = Budget(limit_usd=10)
    assert budget.ensure_affordable(10, 1.0) == pytest.approx(10.0)


def test_ledger_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "budget.json"
    budget = Budget(limit_usd=100)
    budget.record("run-1", 3.25, at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    budget.save(path)
    loaded = Budget.load(path)
    assert loaded.limit_usd == 100
    assert loaded.entries == budget.entries
    assert loaded.entries[0].at == "2026-09-29T12:00:00+00:00"
    assert not path.with_suffix(".json.tmp").exists()


def test_missing_ledger_is_reported(tmp_path: Path) -> None:
    with pytest.raises(BudgetError, match="not found"):
        Budget.load(tmp_path / "none.json")


def test_malformed_ledger_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text('{"limit_usd": 10}', encoding="utf-8")
    with pytest.raises(BudgetError, match="malformed"):
        Budget.load(path)


def test_cli_initialises_records_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = str(tmp_path / "budget.json")
    assert main(["budget", "--file", ledger, "--init", "100"]) == 0
    assert "remaining $100.00" in capsys.readouterr().out
    assert main(["budget", "--file", ledger, "--record", "run-1", "4.5"]) == 0
    assert "spent $4.50  remaining $95.50" in capsys.readouterr().out
    assert main(["budget", "--file", ledger]) == 0
    assert "remaining $95.50" in capsys.readouterr().out


def test_cli_refuses_to_reinitialise_an_existing_ledger(tmp_path: Path) -> None:
    ledger = str(tmp_path / "budget.json")
    assert main(["budget", "--file", ledger, "--init", "100"]) == 0
    assert main(["budget", "--file", ledger, "--init", "50"]) == 2
    assert Budget.load(Path(ledger)).limit_usd == 100


def test_cli_reports_a_missing_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["budget", "--file", str(tmp_path / "none.json")]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_rejects_a_non_numeric_amount(tmp_path: Path) -> None:
    ledger = str(tmp_path / "budget.json")
    main(["budget", "--file", ledger, "--init", "100"])
    assert main(["budget", "--file", ledger, "--record", "run-1", "abc"]) == 2
