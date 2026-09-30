from __future__ import annotations

import io
import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from review_ctx.cli import main
from review_ctx.gate import GATE_FILE, Policy, PolicyError, aggregate_verdict, run_gate, tool_of
from review_ctx.ledger import Ledger
from review_ctx.redact import REDACTED, redact, redact_data
from review_ctx.repo import run_dir
from review_ctx.report import render

FILE = "src/OrderController.java"
QUOTES = {
    1: "package shop;",
    3: "class OrderController {",
    4: "Order get(long id) {",
    5: "return repo.findById(id);",
    7: "void delete(long id) {",
    8: "repo.deleteById(id);",
}


def _policy(**overrides: Any) -> Policy:
    text = resources.files("review_ctx").joinpath("gate_policy.yaml").read_text("utf-8")
    data = yaml.safe_load(text)
    data.update(overrides)
    return Policy.from_dict(data)


@pytest.fixture
def ledger(repo: Path) -> Ledger:
    return Ledger(run_dir(repo, "a1f3", create=True), repo)


def add(
    ledger: Ledger,
    line: int,
    *,
    sev: str = "important",
    lens: str = "security",
    claim: str | None = None,
    src: str | None = None,
    verdict: str | None = None,
    adjusted: str | None = None,
) -> str:
    finding: dict[str, Any] = {
        "lens": lens,
        "sev": sev,
        "loc": {"f": FILE, "l": [line, line]},
        "claim": claim or f"Defect at line {line} for {lens}/{sev}",
        "trigger": "input x leads to wrong behaviour y",
        "trace": [{"f": FILE, "l": line, "q": QUOTES[line]}],
        "fix": "Apply the documented fix",
    }
    if src:
        finding["src"] = src
    result = ledger.emit(json.dumps(finding))
    assert result.ok, result.errors
    if verdict:
        ledger.add_verdict(result.id, verdict, "note", adjusted)
    return result.id


def decision_of(result, finding_id: str):
    return next(d for d in result.decisions if d.id == finding_id)


def test_confirmed_important_finding_is_published_at_e2(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="confirmed")
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.action, decision.tier) == ("publish", "E2")


def test_confirmed_finding_backed_by_a_tool_is_e3(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, src="semgrep:java.spring.missing-authz", verdict="confirmed")
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.action, decision.tier) == ("publish", "E3")


def test_unverified_important_finding_is_degraded_to_a_question(ledger: Ledger) -> None:
    finding_id = add(ledger, 5)
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.action, decision.tier) == ("degrade", "E1")


def test_unverifiable_important_finding_is_degraded(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="unverifiable")
    assert decision_of(run_gate(ledger, _policy()), finding_id).action == "degrade"


def test_refuted_finding_is_dropped_at_e0(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="refuted")
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.action, decision.tier, decision.reason) == ("drop", "E0", "refuted")


def test_unverified_nit_is_published_but_an_unverifiable_one_is_not(ledger: Ledger) -> None:
    plain = add(ledger, 3, sev="nit")
    doubtful = add(ledger, 8, sev="nit", verdict="unverifiable")
    result = run_gate(ledger, _policy())
    assert decision_of(result, plain).action == "publish"
    assert decision_of(result, doubtful).action == "drop"
    assert decision_of(result, doubtful).reason == "unverifiable"


def test_pre_existing_needs_verification_to_be_published(ledger: Ledger) -> None:
    confirmed = add(ledger, 5, sev="pre_existing", verdict="confirmed")
    unverified = add(ledger, 8, sev="pre_existing")
    result = run_gate(ledger, _policy())
    assert decision_of(result, confirmed).action == "publish"
    assert decision_of(result, unverified).action == "drop"


def test_questions_are_held_and_never_published(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, sev="question")
    result = run_gate(ledger, _policy())
    assert decision_of(result, finding_id).action == "hold"
    assert result.published == []


def test_trusted_tool_finding_is_e3_without_a_verdict(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, src="gitleaks:generic-api-key")
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.action, decision.tier) == ("publish", "E3")


def test_verifier_can_adjust_the_severity(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="confirmed", adjusted="nit")
    decision = decision_of(run_gate(ledger, _policy()), finding_id)
    assert (decision.sev, decision.action) == ("nit", "publish")


def test_any_refutation_wins_when_several_verdicts_exist() -> None:
    verdicts = [{"verdict": "confirmed"}, {"verdict": "refuted"}]
    assert aggregate_verdict(verdicts) == ("refuted", None)
    assert aggregate_verdict([{"verdict": "confirmed"}, {"verdict": "unverifiable"}])[0] == (
        "unverifiable"
    )
    assert aggregate_verdict([{"verdict": "confirmed", "sev": "nit"}]) == ("confirmed", "nit")
    assert aggregate_verdict([]) == (None, None)


def test_tool_is_read_from_the_source_field() -> None:
    assert tool_of({"src": "semgrep:rule.id"}) == "semgrep"
    assert tool_of({}) is None


def test_findings_in_ignored_paths_are_dropped(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="confirmed")
    decision = decision_of(run_gate(ledger, _policy(ignore_paths=["src/*"])), finding_id)
    assert (decision.action, decision.reason) == ("drop", "ignored_path")


def test_blocked_calibration_cell_prevents_publication(ledger: Ledger) -> None:
    finding_id = add(ledger, 5, verdict="confirmed")
    policy = _policy(blocked_cells=[{"lens": "security", "tier": "E2"}])
    decision = decision_of(run_gate(ledger, policy), finding_id)
    assert (decision.action, decision.reason) == ("drop", "cell_blocked")


def test_overlapping_findings_of_one_lens_keep_the_best_tier(ledger: Ledger) -> None:
    weak = add(ledger, 5, sev="nit", claim="Weak duplicate")
    strong = add(ledger, 5, claim="Strong duplicate", verdict="confirmed")
    result = run_gate(ledger, _policy())
    assert decision_of(result, strong).action == "publish"
    weak_decision = decision_of(result, weak)
    assert (weak_decision.action, weak_decision.reason) == ("drop", f"duplicate_of:{strong}")


def test_overlapping_findings_of_different_lenses_are_both_kept(ledger: Ledger) -> None:
    first = add(ledger, 5, lens="security", verdict="confirmed")
    second = add(ledger, 5, lens="correctness", verdict="confirmed")
    result = run_gate(ledger, _policy())
    assert {first, second} <= {d.id for d in result.published}


def test_nits_beyond_the_cap_are_suppressed_and_counted(ledger: Ledger) -> None:
    for line in (1, 3, 5, 8):
        add(ledger, line, sev="nit")
    result = run_gate(ledger, _policy(max_nits=2))
    assert len([d for d in result.published if d.sev == "nit"]) == 2
    assert result.nits_suppressed == 2
    assert {d.reason for d in result.with_action("drop")} == {"nit_cap"}


def test_invalid_citation_candidates_are_dropped(ledger: Ledger) -> None:
    bad = {
        "lens": "security",
        "sev": "important",
        "loc": {"f": FILE, "l": [5, 5]},
        "claim": "Made-up quote",
        "trigger": "x",
        "trace": [{"f": FILE, "l": 5, "q": "this line does not exist"}],
    }
    for _ in range(3):
        ledger.emit(json.dumps(bad))
    result = run_gate(ledger, _policy())
    assert [(d.action, d.tier, d.reason) for d in result.decisions] == [
        ("drop", "E0", "invalid_citation")
    ]


def test_published_findings_are_ordered_by_severity_then_tier(ledger: Ledger) -> None:
    nit = add(ledger, 3, sev="nit")
    important = add(ledger, 8, verdict="confirmed")
    ordered = run_gate(ledger, _policy()).decisions
    assert [d.id for d in ordered] == [important, nit]


def test_gate_result_is_persisted_with_every_decision(ledger: Ledger) -> None:
    add(ledger, 5, verdict="refuted")
    add(ledger, 8, verdict="confirmed")
    run_gate(ledger, _policy())
    saved = json.loads((ledger.run_dir / GATE_FILE).read_text(encoding="utf-8"))
    assert len(saved["decisions"]) == 2
    assert saved["counts"]["publish"] == 1 and saved["counts"]["drop"] == 1


def test_default_policy_loads() -> None:
    policy = Policy.load()
    assert policy.max_nits == 5 and "semgrep" in policy.tools


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_nits": -1}, "max_nits"),
        ({"severity_rules": None}, "severity_rules"),
        ({"severity_rules": {"important": {"E3": "publish"}}}, "severity_rules"),
    ],
)
def test_malformed_policy_is_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        _policy(**overrides)


def _run_report(ledger: Ledger, fmt: str) -> str:
    return render(run_gate(ledger, _policy()), fmt)


def test_terminal_report_lists_findings_with_evidence(ledger: Ledger) -> None:
    add(ledger, 5, verdict="confirmed")
    add(ledger, 3, sev="nit")
    text = _run_report(ledger, "terminal")
    assert text.startswith("code-sentinel: 1 important, 0 pre-existing, 1 nit")
    assert "IMPORTANT  src/OrderController.java:5  [security, E2]" in text
    assert "evidence: src/OrderController.java:5  return repo.findById(id);" in text
    assert "fix: Apply the documented fix" in text


def test_markdown_report_has_sections_and_evidence(ledger: Ledger) -> None:
    add(ledger, 5, verdict="confirmed")
    text = _run_report(ledger, "markdown")
    assert "## Important (1)" in text and "## Nit" not in text
    assert "### `src/OrderController.java:5` security (E2)" in text
    assert "- `src/OrderController.java:5  return repo.findById(id);`" in text


def test_json_report_separates_published_questions_and_suppressed(ledger: Ledger) -> None:
    add(ledger, 5, verdict="confirmed")
    add(ledger, 8)
    add(ledger, 3, verdict="refuted")
    data = json.loads(_run_report(ledger, "json"))
    assert len(data["published"]) == 1 and len(data["questions"]) == 1
    assert data["suppressed"][0]["reason"] == "refuted"
    assert data["counts"]["publish"] == 1


def test_report_without_findings_still_renders(ledger: Ledger) -> None:
    assert "0 important" in _run_report(ledger, "terminal")


def test_unknown_report_format_is_rejected(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="format must be one of"):
        _run_report(ledger, "html")


@pytest.mark.parametrize("fmt", ["terminal", "markdown", "json"])
def test_secrets_in_findings_are_redacted_in_every_format(ledger: Ledger, fmt: str) -> None:
    add(ledger, 5, claim='Hardcoded password = "hunter22secret" and key AKIAABCDEFGHIJKLMNOP')
    ledger_id = ledger.findings()[0]["id"]
    ledger.add_verdict(ledger_id, "confirmed", "ok")
    text = _run_report(ledger, fmt)
    assert "hunter22secret" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert REDACTED in text


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "xoxb-1234567890-abcdefghij",
        "sk-" + "a" * 24,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_well_known_secret_formats_are_redacted(secret: str) -> None:
    assert redact(f"value {secret} end") == f"value {REDACTED} end"


def test_secret_assignments_keep_the_key_name() -> None:
    assert redact("api_key: 'abcdef123456'") == f"api_key: '{REDACTED}'"
    assert redact("password=hunter22") == f"password={REDACTED}"


def test_ordinary_text_is_not_redacted() -> None:
    text = "Token bucket algorithm in class OrderController; password policy documented"
    assert redact(text) == text


def _cli(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return main([*args, "--run", "a1f3", "--repo", str(repo)])


def test_cli_gate_prints_counts_and_report_writes_a_file(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    ledger: Ledger,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    add(ledger, 5, verdict="confirmed")
    assert _cli(monkeypatch, repo, "gate") == 0
    assert json.loads(capsys.readouterr().out)["publish"] == 1
    out = tmp_path / "report.md"
    assert _cli(monkeypatch, repo, "report", "--format", "markdown", "--out", str(out)) == 0
    assert "## Important (1)" in out.read_text(encoding="utf-8")


def test_cli_report_defaults_to_the_terminal_format(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    ledger: Ledger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add(ledger, 5, verdict="confirmed")
    assert _cli(monkeypatch, repo, "report") == 0
    assert capsys.readouterr().out.startswith("code-sentinel: 1 important")


def test_cli_reports_a_broken_policy_file(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    ledger: Ledger,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("max_nits: 3\n", encoding="utf-8")
    assert _cli(monkeypatch, repo, "gate", "--policy", str(policy)) == 2
    assert "severity_rules" in capsys.readouterr().err


def test_redaction_reaches_nested_json_values() -> None:
    data = {"a": ['password = "hunter22secret"', {"b": "AKIAABCDEFGHIJKLMNOP"}], "n": 3}
    assert redact_data(data) == {
        "a": [f'password = "{REDACTED}"', {"b": REDACTED}],
        "n": 3,
    }
