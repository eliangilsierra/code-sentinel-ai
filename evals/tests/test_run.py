from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.runner.__main__ import main
from evals.runner.budget import Budget
from evals.runner.claude_cli import parse_result, run_claude
from evals.runner.fixtures import materialize
from evals.runner.run import (
    RunError,
    RunOptions,
    load_case,
    pending_cases,
    resolve_cases,
    run_suite,
)
from evals.runner.sut import (
    CodeReviewSut,
    ReviewSquadSut,
    SutConfig,
    finding_from_entry,
    latest_run,
)
from evals.tests.conftest import Fixture

FAKE_CLAUDE = """
import json
import os
import subprocess
import sys

args = sys.argv[1:]
counter = os.environ.get("FAKE_COUNTER")
if counter:
    with open(counter, "a") as handle:
        handle.write("call\\n")
prompt = args[args.index("-p") + 1]
usage = {
    "claude-sonnet-5-5": {
        "inputTokens": 1000,
        "outputTokens": 200,
        "cacheReadInputTokens": 5000,
        "cacheCreationInputTokens": 0,
        "costUSD": 0.01,
    }
}


def out(**extra):
    print(json.dumps({"is_error": False, "num_turns": 2, "modelUsage": usage, **extra}))


if os.environ.get("FAKE_FAIL"):
    print(json.dumps({"is_error": True, "result": "usage limit reached"}))
    sys.exit(1)
if "--json-schema" in args:
    out(structured_output=json.loads(os.environ["FAKE_EXTRACTED"]), result="")
elif prompt.startswith("/review-squad:review"):
    core = [sys.executable, "-m", "review_ctx.cli"]
    subprocess.run(core + ["prepare", "--id", "a1f3"], check=True, capture_output=True)
    finding = json.loads(os.environ["FAKE_FINDING"])
    emitted = subprocess.run(
        core + ["emit", "--run", "a1f3"],
        input=json.dumps(finding),
        text=True,
        capture_output=True,
        check=True,
    )
    finding_id = json.loads(emitted.stdout)["id"]
    subprocess.run(
        core + ["verdict", "--run", "a1f3", finding_id, "confirmed", "--note", "ok"],
        check=True,
        capture_output=True,
    )
    out(result="reviewed")
elif prompt.startswith("/code-review"):
    out(result="src/OrderController.java:3 possible bug")
"""

TRUE_FINDING = {
    "lens": "correctness",
    "sev": "important",
    "loc": {"f": "src/OrderController.java", "l": [3, 4]},
    "claim": "list returns nothing",
    "trigger": "GET /orders -> empty body",
    "trace": [{"f": "src/OrderController.java", "l": 3, "q": "void list() {}"}],
    "fix": "Return the orders",
}
STRAY_FINDING = {
    **TRUE_FINDING,
    "loc": {"f": "src/OrderController.java", "l": [1, 1]},
    "claim": "class declaration is odd",
    "trace": [{"f": "src/OrderController.java", "l": 1, "q": "class OrderController {"}],
}


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "calls.txt"))
    monkeypatch.setenv("FAKE_FINDING", json.dumps(TRUE_FINDING))
    return [sys.executable, str(script)]


def _calls(tmp_path: Path) -> int:
    path = tmp_path / "calls.txt"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def _case(shop: Fixture, case_id: str = "shop-001", **changes: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "category": "bug",
        "stack": ["java"],
        "mode": "pr",
        "fixture": shop.name,
        "base": shop.base,
        "head": shop.head,
        "source": "synthetic:mutation/example",
        "expected": [
            {
                "lens": "correctness",
                "sev": "important",
                "loc": {"f": "src/OrderController.java", "l": [3, 4]},
            }
        ],
        "must_not": [],
        "adversarial": False,
    }
    case.update(changes)
    return case


def _write(directory: Path, case: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case['id']}.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path, shop_fixture: Fixture) -> dict[str, Path]:
    cases = tmp_path / "cases"
    _write(cases / "java", _case(shop_fixture, "shop-001"))
    _write(cases / "java", _case(shop_fixture, "shop-002"))
    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "ws.txt").write_text("# walking skeleton\nshop-001\n", encoding="utf-8")
    return {
        "cases": cases,
        "suites": suites,
        "fixtures": shop_fixture.fixtures_dir,
        "reports": tmp_path / "reports",
    }


def _options(workspace: dict[str, Path], suite: str = "all", **changes: Any) -> RunOptions:
    return RunOptions(
        suite=suite,
        run_id="r1",
        cases_dir=workspace["cases"],
        fixtures_dir=workspace["fixtures"],
        suites_dir=workspace["suites"],
        reports_dir=workspace["reports"],
        **changes,
    )


def test_parse_result_reads_the_whole_output_or_its_last_json_line() -> None:
    assert parse_result('{"a": 1}') == {"a": 1}
    assert parse_result('warning\n{"a": 2}\n') == {"a": 2}
    assert parse_result("not json") is None
    assert parse_result("[1, 2]") is None


def test_run_claude_reports_a_missing_command_and_a_timeout(tmp_path: Path) -> None:
    missing = run_claude(["definitely-not-a-command-xyz"], tmp_path)
    assert missing.failed and missing.returncode == 127
    slow = run_claude([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=0.5)
    assert slow.failed and "timed out" in slow.message


def test_error_result_counts_as_a_failure(tmp_path: Path) -> None:
    result = run_claude(
        [sys.executable, "-c", 'print(\'{"is_error": true, "result": "limit"}\')'], tmp_path
    )
    assert result.returncode == 0 and result.failed and result.message == "limit"


def test_review_squad_sut_collects_the_published_findings(
    shop_fixture: Fixture, tmp_path: Path, fake_claude: list[str]
) -> None:
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    sut = ReviewSquadSut(SutConfig(claude=fake_claude))
    outcome = sut.run(repo, _case(shop_fixture))
    assert outcome.error is None
    assert [(f.f, f.start, f.end, f.sev, f.lens) for f in outcome.findings] == [
        ("src/OrderController.java", 3, 4, "important", "correctness")
    ]
    assert outcome.usages[0].model == "claude-sonnet-5-5" and outcome.usages[0].output == 200
    assert latest_run(repo) == "a1f3"


def test_review_squad_command_uses_the_plugin_and_the_budget_cap() -> None:
    config = SutConfig(plugin_dir=Path("/plugins/rs"), cap_usd=0.25, effort="low")
    command = ReviewSquadSut(config).command()
    assert command[:3] == ["claude", "-p", "/review-squad:review"]
    assert command[command.index("--max-budget-usd") + 1] == "0.25"
    assert command[command.index("--plugin-dir") + 1] == str(Path("/plugins/rs"))
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--effort") + 1] == "low"


def test_environment_isolates_the_configuration_directory(tmp_path: Path) -> None:
    assert SutConfig(config_dir=tmp_path).environment() == {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert SutConfig().environment() == {}


def test_failed_review_records_the_error_and_no_findings(
    shop_fixture: Fixture,
    tmp_path: Path,
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FAIL", "1")
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    outcome = ReviewSquadSut(SutConfig(claude=fake_claude)).run(repo, _case(shop_fixture))
    assert outcome.error == "usage limit reached" and outcome.findings == []


def test_latest_run_is_none_without_runs(tmp_path: Path) -> None:
    assert latest_run(tmp_path) is None
    (tmp_path / ".git" / "review-squad" / "runs").mkdir(parents=True)
    assert latest_run(tmp_path) is None


def test_finding_from_entry_reads_location_and_lens() -> None:
    finding = finding_from_entry(
        {"loc": {"f": "A.java", "l": [4, 6]}, "sev": "nit", "claim": "c", "lens": "security"}
    )
    assert (finding.f, finding.start, finding.end, finding.sev, finding.lens) == (
        "A.java",
        4,
        6,
        "nit",
        "security",
    )


def test_code_review_sut_extracts_defects_and_ignores_cleanups(
    shop_fixture: Fixture,
    tmp_path: Path,
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extracted = {
        "findings": [
            {
                "f": "src/OrderController.java",
                "l": 3,
                "claim": "bug",
                "sev": "important",
                "category": "defect",
            },
            {
                "f": "src/OrderController.java",
                "l": 4,
                "claim": "reuse",
                "sev": "nit",
                "category": "cleanup",
            },
        ]
    }
    monkeypatch.setenv("FAKE_EXTRACTED", json.dumps(extracted))
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    outcome = CodeReviewSut(SutConfig(claude=fake_claude)).run(repo, _case(shop_fixture))
    assert outcome.error is None
    assert [(f.start, f.claim) for f in outcome.findings] == [(3, "bug")]
    assert outcome.extra_usages and outcome.usages


def test_code_review_sut_reports_an_extraction_failure(
    shop_fixture: Fixture,
    tmp_path: Path,
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_EXTRACTED", "null")
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    outcome = CodeReviewSut(SutConfig(claude=fake_claude)).run(repo, _case(shop_fixture))
    assert outcome.error and "could not extract" in outcome.error


def test_suites_resolve_from_files_all_and_globs(workspace: dict[str, Path]) -> None:
    def names(suite: str) -> list[str]:
        paths = resolve_cases(suite, workspace["cases"], workspace["suites"])
        return [p.stem for p in paths]

    assert names("ws") == ["shop-001"]
    assert names("all") == ["shop-001", "shop-002"]
    assert names("shop-00[12]") == ["shop-001", "shop-002"]
    with pytest.raises(RunError, match="matches no case"):
        names("nothing-*")
    (workspace["suites"] / "broken.txt").write_text("shop-001\nghost\n", encoding="utf-8")
    with pytest.raises(RunError, match="unknown cases: ghost"):
        names("broken")


def test_invalid_case_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("id: only-an-id\n", encoding="utf-8")
    with pytest.raises(RunError, match="invalid case"):
        load_case(path)
    path.write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(RunError, match="cannot read case"):
        load_case(path)


def test_suite_run_scores_cases_and_writes_the_summary(
    workspace: dict[str, Path], tmp_path: Path, fake_claude: list[str]
) -> None:
    sut = ReviewSquadSut(SutConfig(claude=fake_claude))
    result = run_suite(_options(workspace), sut)
    assert result.errors == [] and len(result.scores) == 2
    metrics = json.loads((result.directory / "summary.json").read_text(encoding="utf-8"))
    assert (metrics["metrics"]["tp"], metrics["metrics"]["fp"], metrics["metrics"]["fn"]) == (
        2,
        0,
        0,
    )
    assert metrics["metrics"]["f05"] == 1.0
    assert result.total_cost_usd == pytest.approx(
        2 * (1000 * 2 + 200 * 10) / 1e6 + 2 * 5000 * 0.2 / 1e6
    )
    stored = json.loads(
        (result.directory / "shop-001" / "outcome.json").read_text(encoding="utf-8")
    )
    assert stored["cache_read_ratio"] == pytest.approx(5000 / 6000) and stored["error"] is None
    assert (result.directory / "shop-001" / "claude.json").is_file()


def test_a_finding_far_from_the_expected_location_is_a_false_positive(
    workspace: dict[str, Path],
    shop_fixture: Fixture,
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strict = _case(shop_fixture, "shop-001")
    strict["expected"][0]["tol"] = 0
    _write(workspace["cases"] / "java", strict)
    monkeypatch.setenv("FAKE_FINDING", json.dumps(STRAY_FINDING))
    result = run_suite(_options(workspace, "ws"), ReviewSquadSut(SutConfig(claude=fake_claude)))
    (record,) = result.summary.cases
    assert (record.tp, record.fp, record.fn) == (0, 1, 1)


def test_stored_outcomes_are_reused_unless_forced(
    workspace: dict[str, Path], tmp_path: Path, fake_claude: list[str]
) -> None:
    sut = ReviewSquadSut(SutConfig(claude=fake_claude))
    run_suite(_options(workspace), sut)
    assert _calls(tmp_path) == 2
    again = run_suite(_options(workspace), sut)
    assert _calls(tmp_path) == 2 and all(r.reused for r in again.results)
    assert again.total_cost_usd == 0 and len(again.scores) == 2
    assert pending_cases(_options(workspace), sut.name) == 0
    run_suite(_options(workspace, force=True), sut)
    assert _calls(tmp_path) == 4
    assert pending_cases(_options(workspace, force=True), sut.name) == 2


def test_failed_cases_are_reported_not_scored_and_retried_later(
    workspace: dict[str, Path],
    tmp_path: Path,
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sut = ReviewSquadSut(SutConfig(claude=fake_claude))
    monkeypatch.setenv("FAKE_FAIL", "1")
    failed = run_suite(_options(workspace, "ws"), sut)
    assert failed.scores == [] and [c for c, _ in failed.errors] == ["shop-001"]
    assert pending_cases(_options(workspace, "ws"), sut.name) == 1
    monkeypatch.delenv("FAKE_FAIL")
    retried = run_suite(_options(workspace, "ws"), sut)
    assert retried.errors == [] and len(retried.scores) == 1


def _cli(workspace: dict[str, Path], claude: list[str], *extra: str) -> int:
    return main(
        [
            "run",
            "--suite",
            "all",
            "--run-id",
            "r9",
            "--claude",
            " ".join(f'"{part}"' for part in claude),
            "--cases-dir",
            str(workspace["cases"]),
            "--fixtures-dir",
            str(workspace["fixtures"]),
            "--suites-dir",
            str(workspace["suites"]),
            "--reports-dir",
            str(workspace["reports"]),
            "--plugin-dir",
            str(workspace["cases"]),
            *extra,
        ]
    )


def test_cli_charges_the_budget_and_prints_the_summary(
    workspace: dict[str, Path],
    tmp_path: Path,
    fake_claude: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "budget.json"
    Budget(limit_usd=100).save(ledger)
    assert _cli(workspace, fake_claude, "--budget-file", str(ledger)) == 0
    output = capsys.readouterr().out
    assert "run r9  sut review-squad  cases 2" in output and "F0.5 1.000" in output
    assert Budget.load(ledger).spent_usd > 0


def test_cli_refuses_a_run_the_budget_cannot_cover(
    workspace: dict[str, Path],
    tmp_path: Path,
    fake_claude: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "budget.json"
    Budget(limit_usd=0.10).save(ledger)
    assert _cli(workspace, fake_claude, "--budget-file", str(ledger)) == 2
    assert "exceeds the remaining" in capsys.readouterr().err
    assert _calls(tmp_path) == 0


def test_cli_requires_a_ledger_unless_budgeting_is_disabled(
    workspace: dict[str, Path],
    tmp_path: Path,
    fake_claude: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "none.json"
    assert _cli(workspace, fake_claude, "--budget-file", str(missing)) == 2
    assert "not found" in capsys.readouterr().err
    assert _cli(workspace, fake_claude, "--no-budget") == 0


def test_cli_exits_one_when_a_case_fails(
    workspace: dict[str, Path],
    fake_claude: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_FAIL", "1")
    assert _cli(workspace, fake_claude, "--no-budget") == 1
    assert "error shop-001: usage limit reached" in capsys.readouterr().err


def test_cli_reports_an_unknown_suite(
    workspace: dict[str, Path], fake_claude: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = _cli(workspace, fake_claude, "--no-budget", "--suite", "ghost-*")
    assert code == 2 and "matches no case" in capsys.readouterr().err
