from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest
from review_ctx.cli import main
from review_ctx.ledger import MAX_ATTEMPTS, Ledger, LedgerError, fingerprint, normalize
from review_ctx.repo import RepoError, run_dir
from review_ctx.show import render_finding

FINDING: dict[str, Any] = {
    "lens": "security",
    "sev": "important",
    "loc": {"f": "src/OrderController.java", "l": [4, 6]},
    "claim": "Endpoint returns any order without an ownership check",
    "trigger": "GET /orders/7 as user B returns the order of user A",
    "trace": [{"f": "src/OrderController.java", "l": 5, "q": "return repo.findById(id);"}],
    "fix": "Filter by the authenticated tenant",
}


def _finding(**changes: Any) -> dict[str, Any]:
    finding = copy.deepcopy(FINDING)
    finding.update(changes)
    return finding


def _emit(ledger: Ledger, finding: dict[str, Any]):
    return ledger.emit(json.dumps(finding))


@pytest.fixture
def ledger(repo: Path) -> Ledger:
    return Ledger(run_dir(repo, "a1f3", create=True), repo)


def test_valid_finding_is_accepted_and_recorded(ledger: Ledger) -> None:
    result = _emit(ledger, FINDING)
    assert result.ok and result.status == "accepted" and not result.line_corrected
    assert len(result.id) == 10
    records = ledger.findings()
    assert [r["id"] for r in records] == [result.id]
    assert records[0]["status"] == "candidate"


def test_emitting_the_same_finding_twice_records_it_once(ledger: Ledger) -> None:
    first = _emit(ledger, FINDING)
    second = _emit(ledger, FINDING)
    assert second.status == "duplicate" and second.id == first.id
    assert len(ledger.findings()) == 1


def test_fingerprint_depends_on_lens_claim_and_location(ledger: Ledger) -> None:
    base = fingerprint(FINDING)
    assert fingerprint(_finding(lens="correctness")) != base
    assert fingerprint(_finding(claim="A different defect")) != base
    moved = _finding(loc={"f": "src/OrderController.java", "l": [24, 26]})
    assert fingerprint(moved) != base
    assert (
        fingerprint(_finding(claim="  ENDPOINT returns any order   without an ownership check"))
        == base
    )


def test_invalid_json_is_rejected_with_a_message(ledger: Ledger) -> None:
    result = ledger.emit("{not json")
    assert result.status == "rejected" and "not valid JSON" in result.errors[0]


def test_schema_violations_are_rejected(ledger: Ledger) -> None:
    finding = _finding()
    del finding["trigger"]
    result = _emit(ledger, finding)
    assert result.status == "rejected" and "trigger" in result.errors[0]
    assert ledger.findings() == []


def test_self_declared_confidence_is_rejected(ledger: Ledger) -> None:
    assert _emit(ledger, _finding(confidence=0.9)).status == "rejected"


def test_citation_with_different_whitespace_is_accepted(ledger: Ledger) -> None:
    finding = _finding()
    finding["trace"][0]["q"] = "return   repo.findById(id);"
    assert _emit(ledger, finding).ok


def test_citation_within_the_window_is_corrected(ledger: Ledger) -> None:
    finding = _finding()
    finding["trace"][0]["l"] = 7
    result = _emit(ledger, finding)
    assert result.ok and result.line_corrected
    assert ledger.get(result.id)["finding"]["trace"][0]["l"] == 5
    assert ledger.get(result.id)["line_corrected"] is True


def test_citation_outside_the_window_is_rejected_with_a_suggestion(ledger: Ledger) -> None:
    finding = _finding()
    finding["trace"][0]["q"] = "return repo.findAll();"
    finding["trace"][0]["l"] = 5
    result = _emit(ledger, finding)
    assert result.status == "rejected"
    assert "quote not found near src/OrderController.java:5" in result.errors[0]
    assert "closest line 5: 'return repo.findById(id);'" in result.errors[0]


def test_quote_far_from_any_line_gets_no_suggestion(ledger: Ledger) -> None:
    finding = _finding()
    finding["trace"][0]["q"] = "zzzzzzzzzzzzzzzzzzzz"
    result = _emit(ledger, finding)
    assert "closest line" not in result.errors[0]


def test_citation_of_a_missing_file_is_rejected(ledger: Ledger) -> None:
    finding = _finding()
    finding["trace"][0]["f"] = "src/Missing.java"
    assert "does not exist" in _emit(ledger, finding).errors[0]


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:/Windows/win.ini"])
def test_paths_outside_the_repository_are_rejected(ledger: Ledger, path: str) -> None:
    finding = _finding()
    finding["trace"][0]["f"] = path
    result = _emit(ledger, finding)
    assert result.status == "rejected"
    assert "inside the repository" in result.errors[0] or "does not exist" in result.errors[0]
    assert ledger.findings() == []


def test_location_beyond_the_end_of_the_file_is_rejected(ledger: Ledger) -> None:
    result = _emit(ledger, _finding(loc={"f": "src/OrderController.java", "l": [4, 99]}))
    assert "has 10 lines" in result.errors[0]


def test_reversed_location_is_rejected(ledger: Ledger) -> None:
    result = _emit(ledger, _finding(loc={"f": "src/OrderController.java", "l": [6, 4]}))
    assert "greater than end" in result.errors[0]


def test_every_problem_is_reported_together(ledger: Ledger) -> None:
    finding = _finding(loc={"f": "src/Missing.java", "l": [1, 2]})
    finding["trace"][0]["q"] = "nothing like this"
    assert len(_emit(ledger, finding).errors) == 2


def test_third_failed_attempt_is_recorded_as_invalid_citation(ledger: Ledger) -> None:
    bad = _finding()
    bad["trace"][0]["q"] = "return repo.findAll();"
    results = [_emit(ledger, bad) for _ in range(MAX_ATTEMPTS)]
    assert [r.status for r in results] == ["rejected", "rejected", "abandoned"]
    assert "do not retry" in results[-1].errors[-1]
    assert [r["status"] for r in ledger.findings()] == ["invalid_citation"]


def test_a_corrected_retry_is_accepted_before_the_limit(ledger: Ledger) -> None:
    bad = _finding()
    bad["trace"][0]["q"] = "return repo.findAll();"
    assert _emit(ledger, bad).status == "rejected"
    assert _emit(ledger, FINDING).status == "accepted"


def test_verdicts_are_appended_and_validated(ledger: Ledger) -> None:
    finding_id = _emit(ledger, FINDING).id
    ledger.add_verdict(finding_id, "confirmed", "reproduced", severity="nit")
    ledger.add_verdict(finding_id, "unverifiable", "second look")
    verdicts = ledger.verdicts()
    assert [v["verdict"] for v in verdicts] == ["confirmed", "unverifiable"]
    assert verdicts[0]["sev"] == "nit" and "sev" not in verdicts[1]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("nope", "confirmed", ""), "unknown finding id"),
        (("ID", "maybe", ""), "verdict must be one of"),
        (("ID", "confirmed", "x" * 121), "the limit is 120"),
        (("ID", "confirmed", "", "critical"), "severity must be one of"),
    ],
)
def test_invalid_verdicts_are_rejected(ledger: Ledger, args: tuple[str, ...], message: str) -> None:
    finding_id = _emit(ledger, FINDING).id
    resolved = tuple(finding_id if a == "ID" else a for a in args)
    with pytest.raises(LedgerError, match=message):
        ledger.add_verdict(*resolved)
    assert ledger.verdicts() == []


def test_show_renders_the_finding_with_code_context(ledger: Ledger) -> None:
    finding_id = _emit(ledger, FINDING).id
    ledger.add_verdict(finding_id, "refuted", "guarded by a filter")
    text = render_finding(ledger, finding_id)
    assert f"id: {finding_id}" in text
    assert "loc: src/OrderController.java:4-6" in text
    assert "src/OrderController.java:5  return repo.findById(id);" in text
    assert "verdict: refuted  guarded by a filter" in text
    assert "5:         return repo.findById(id);" in text


def test_show_prefers_the_packet_hunk_and_truncates_long_code(ledger: Ledger) -> None:
    finding_id = _emit(ledger, FINDING).id
    hunk = {
        "id": "h1",
        "p": "src/OrderController.java",
        "scope": "get(long id)",
        "l": [1, 10],
        "code": "x" * 9000,
    }
    (ledger.run_dir / "packet.json").write_text(json.dumps({"hunks": [hunk]}), encoding="utf-8")
    text = render_finding(ledger, finding_id)
    assert "hunk h1 scope: get(long id) lines 1-10" in text
    assert text.endswith("... truncated")


def test_unknown_finding_cannot_be_shown(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="unknown finding id"):
        render_finding(ledger, "missing")


def test_normalize_collapses_whitespace() -> None:
    assert normalize("  a \t b\n c ") == "a b c"


def test_run_ids_are_validated_and_must_exist(repo: Path) -> None:
    with pytest.raises(RepoError, match="invalid run id"):
        run_dir(repo, "../x")
    with pytest.raises(RepoError, match="not found"):
        run_dir(repo, "abc123")


def _cli(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str, stdin: str = "") -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main([*args, "--run", "a1f3", "--repo", str(repo)])


def test_cli_emit_show_and_verdict_round_trip(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, repo, "emit", stdin=json.dumps(FINDING)) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "accepted"
    assert _cli(monkeypatch, repo, "show", emitted["id"]) == 0
    assert "claim: Endpoint returns any order" in capsys.readouterr().out
    assert _cli(monkeypatch, repo, "verdict", emitted["id"], "confirmed", "--note", "ok") == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "confirmed"


def test_cli_emit_reports_rejections_on_stderr_with_exit_one(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _finding()
    bad["trace"][0]["q"] = "return repo.findAll();"
    assert _cli(monkeypatch, repo, "emit", stdin=json.dumps(bad)) == 1
    assert "error: trace[0]: quote not found" in capsys.readouterr().err


def test_cli_emit_exits_three_when_a_finding_is_abandoned(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    bad = _finding()
    bad["trace"][0]["q"] = "return repo.findAll();"
    codes = [_cli(monkeypatch, repo, "emit", stdin=json.dumps(bad)) for _ in range(MAX_ATTEMPTS)]
    assert codes == [1, 1, 3]


def test_cli_accepts_quotes_dollars_and_backticks_from_stdin(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = _finding(claim='Uses "$HOME" and `id` in a shell string; it\'s unsafe')
    assert _cli(monkeypatch, repo, "emit", stdin=json.dumps(finding)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"


def test_cli_show_of_an_unknown_run_exits_two(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, repo, "show", "abc") == 2
    assert "not found" in capsys.readouterr().err


def test_cli_outside_a_repository_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, tmp_path, "emit", stdin="{}") == 2
    assert "not a git repository" in capsys.readouterr().err
