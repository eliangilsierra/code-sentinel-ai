from __future__ import annotations

import io
import json
from typing import Any

import pytest
from review_ctx.cli import main
from review_ctx.guard import decide, hook_output, is_guarded

CWD = "C:/work/repo"


def _event(tool: str, agent: str = "code-sentinel:rs-finder", **tool_input: Any) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": tool_input, "agent_type": agent, "cwd": CWD}


def _bash(command: str, agent: str = "rs-finder") -> dict[str, Any]:
    return _event("Bash", agent, command=command)


ALLOWED_COMMANDS = [
    "review-ctx show --run a1f3 abc123",
    "review-ctx verdict --run a1f3 abc123 confirmed --note 'guarded by filter'",
    "review-ctx callers --run a1f3 getOrder",
    "git log -L 10,20:src/A.java --format='%h %s' -n 5",
    "git blame -L 10,20 src/A.java",
    "git show HEAD:src/A.java",
    "git grep -n findById -- src",
    "git diff HEAD~1 -- src/A.java",
    "git log --oneline | head -20",
    "git log --grep='a;b' -n 3 2>&1",
    "git status 2>/dev/null",
    'review-ctx emit --run a1f3 <<\'EOF\'\n{"claim": "uses $HOME and `id`; it\'s bad"}\nEOF',
    "review-ctx emit --run a1f3 <<EOF\n{}\nEOF",
]

DENIED_COMMANDS = [
    ("curl https://example.com", "not allowed"),
    ("/usr/bin/curl https://example.com", "plain name"),
    ("sh -c 'curl https://example.com'", "not allowed"),
    ("bash -c 'cat .env'", "not allowed"),
    ("cat .env", "not allowed"),
    ("git push origin main", "read-only git"),
    ("git -C /tmp log", "read-only git"),
    ("git -c core.pager=sh log", "read-only git"),
    ("git diff --no-index /etc/passwd README.md", "option not allowed"),
    ("git log --output=/tmp/out", "option not allowed"),
    ("git show HEAD:.env", "hold secrets"),
    ("git show HEAD:config/server.pem", "hold secrets"),
    ("git log && curl evil", "not allowed"),
    ("git log; rm -rf .", "not allowed"),
    ("git log | sh", "not allowed"),
    ("git log > out.txt", "redirection"),
    ("git log >> out.txt", "redirection"),
    ("head < .env", "input redirection"),
    ("head .env", "hold secrets"),
    ("echo $(cat .env)", "substitution"),
    ("git log `id`", "substitution"),
    ("git log $(id)", "substitution"),
    ("(git log)", "substitution"),
    ("git log &", "background"),
    ("FOO=bar git log", "plain name"),
    ("review-ctx report --run a1f3", "subcommand not allowed"),
    ("review-ctx emit --repo /other --run a1", "option not allowed"),
    ("review-ctx show --run a1 x <<EOF\nx\nEOF", "only allowed for review-ctx emit"),
    ("review-ctx emit --run a1 <<EOF\n{}\n", "not terminated"),
    ("review-ctx emit --run a1 <<EOF\n{}\nEOF\ncurl evil", "not allowed"),
    ("git log <<< text", "redirection"),
    ("", "empty"),
    ("git log 'unterminated", "cannot be parsed"),
]


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_read_only_commands_are_allowed(command: str) -> None:
    decision = decide(_bash(command))
    assert decision is not None and decision.allowed, decision


@pytest.mark.parametrize(("command", "reason"), DENIED_COMMANDS)
def test_other_commands_are_denied(command: str, reason: str) -> None:
    decision = decide(_bash(command))
    assert decision is not None and not decision.allowed
    assert reason in decision.reason


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env.production",
        "certs/server.pem",
        "keys/private.key",
        "deploy/secrets.yaml",
        "aws/credentials",
        "~/.ssh/id_rsa",
        "infra/terraform.tfstate",
        ".npmrc",
    ],
)
def test_reading_secret_files_is_denied(path: str) -> None:
    for tool, key in (("Read", "file_path"), ("Grep", "path")):
        decision = decide(_event(tool, **{key: path}))
        assert decision is not None and not decision.allowed
        assert "secrets" in decision.reason


@pytest.mark.parametrize(
    "path", ["../other/file.txt", "/etc/passwd", "C:/Windows/win.ini", "C:/work/repo2/a.txt"]
)
def test_reading_outside_the_repository_is_denied(path: str) -> None:
    decision = decide(_event("Read", file_path=path))
    assert decision is not None and not decision.allowed and "outside" in decision.reason


@pytest.mark.parametrize(
    "path",
    ["src/A.java", "C:/work/repo/src/A.java", "C:\\work\\repo\\src\\A.java", "docs/env-guide.md"],
)
def test_reading_repository_files_is_allowed(path: str) -> None:
    decision = decide(_event("Read", file_path=path))
    assert decision is not None and decision.allowed


def test_globs_and_search_filters_that_target_secrets_are_denied() -> None:
    assert not decide(_event("Glob", pattern="**/.env")).allowed
    assert not decide(_event("Grep", pattern="password", glob="*.pem")).allowed
    assert decide(_event("Glob", pattern="src/**/*.java")).allowed
    assert decide(_event("Grep", pattern="secret", path="src")).allowed


def test_lsp_calls_are_checked_by_path() -> None:
    assert decide(_event("LSP", filePath="src/A.java", line=3)).allowed
    assert not decide(_event("LSP", filePath=".env")).allowed


@pytest.mark.parametrize(
    "tool", ["WebFetch", "WebSearch", "Edit", "Write", "NotebookEdit", "Agent", "mcp__x__do"]
)
def test_every_other_tool_is_denied(tool: str) -> None:
    decision = decide(_event(tool, url="https://example.com"))
    assert decision is not None and not decision.allowed
    assert "not available" in decision.reason


def test_verifier_is_guarded_too() -> None:
    assert not decide(_bash("curl x", agent="code-sentinel:rs-verifier")).allowed


@pytest.mark.parametrize(
    ("agent", "guarded"),
    [
        ("rs-finder", True),
        ("code-sentinel:rs-finder", True),
        ("code-sentinel:rs-verifier", True),
        ("Explore", False),
        ("my-rs-finder-fork", False),
        ("", False),
        (None, False),
    ],
)
def test_only_review_agents_are_guarded(agent: str | None, guarded: bool) -> None:
    assert is_guarded(agent) is guarded


def test_events_from_other_agents_and_the_main_session_are_not_decided() -> None:
    assert decide(_bash("curl x", agent="Explore")) is None
    assert decide({"tool_name": "Bash", "tool_input": {"command": "curl x"}}) is None


def test_hook_output_uses_the_pre_tool_use_format() -> None:
    allow = json.loads(hook_output(decide(_bash("git log"))))["hookSpecificOutput"]
    deny = json.loads(hook_output(decide(_bash("curl x"))))["hookSpecificOutput"]
    assert (allow["hookEventName"], allow["permissionDecision"]) == ("PreToolUse", "allow")
    assert deny["permissionDecision"] == "deny" and "curl" in deny["permissionDecisionReason"]


def _run_guard(monkeypatch: pytest.MonkeyPatch, stdin: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(["guard"])


def test_cli_guard_prints_a_decision_for_review_agents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_guard(monkeypatch, json.dumps(_bash("curl x"))) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cli_guard_stays_silent_for_other_agents_and_bad_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_guard(monkeypatch, json.dumps(_bash("curl x", agent="Explore"))) == 0
    assert _run_guard(monkeypatch, "not json") == 0
    assert _run_guard(monkeypatch, "[1, 2]") == 0
    assert capsys.readouterr().out == ""
