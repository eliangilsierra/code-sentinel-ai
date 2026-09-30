"""Read-only enforcement for review subagents, applied to ``PreToolUse`` hook events."""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

GUARDED_AGENTS = ("rs-finder", "rs-verifier")
GIT_READ_COMMANDS = frozenset(
    {"log", "show", "blame", "grep", "diff", "status", "ls-files", "rev-parse", "cat-file"}
)
GIT_DENIED_FLAGS = frozenset(
    {"--output", "--ext-diff", "--textconv", "--open-files-in-pager", "-O", "--no-index"}
)
REVIEW_CTX_COMMANDS = frozenset({"emit", "show", "verdict", "callers"})
REVIEW_CTX_DENIED_FLAGS = frozenset({"--repo", "--policy", "--out"})
TEXT_FILTERS = frozenset({"head", "tail", "wc", "sort", "uniq"})
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LSP"})
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*secret*",
    "*credential*",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "*.tfstate",
)
SEPARATORS = frozenset({"&&", "||", "|", ";"})
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


ALLOWED = Decision(True, "read-only review operation")


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


def is_guarded(agent_type: str | None) -> bool:
    """Whether ``agent_type`` names a review subagent, with or without a plugin prefix."""
    return bool(agent_type) and agent_type.split(":")[-1] in GUARDED_AGENTS


def _is_secret_path(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


def _outside(path: str, cwd: str | None) -> bool:
    normalized = path.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        return True
    if not (normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized)):
        return False
    if not cwd:
        return True
    base = cwd.replace("\\", "/").rstrip("/").lower()
    target = normalized.lower()
    return not (target == base or target.startswith(base + "/"))


def _check_path(path: str, cwd: str | None) -> Decision | None:
    if _is_secret_path(path):
        return _deny(f"reading {path!r} is not allowed: it may hold secrets")
    if _outside(path, cwd):
        return _deny(f"{path!r} is outside the repository")
    return None


def _read_tool(tool: str, tool_input: dict[str, Any], cwd: str | None) -> Decision:
    for key, value in tool_input.items():
        if not isinstance(value, str):
            continue
        name = key.lower()
        if "path" in name or "file" in name:
            problem = _check_path(value, cwd)
            if problem:
                return problem
        elif (tool == "Glob" and name == "pattern") or (tool == "Grep" and name == "glob"):
            if _is_secret_path(value):
                return _deny(f"{value!r} could match files that hold secrets")
    return ALLOWED


def _visible_lines(command: str) -> list[str] | None:
    """Lines of ``command`` without heredoc bodies; ``None`` if a heredoc is not terminated."""
    lines = command.split("\n")
    visible: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        visible.append(line)
        index += 1
        match = _HEREDOC.search(line)
        if match:
            while index < len(lines) and lines[index].strip() != match.group(2):
                index += 1
            if index >= len(lines):
                return None
            index += 1
    return [line for line in visible if line.strip()]


def _tokens(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _commands(line: str) -> tuple[list[tuple[list[str], bool]] | None, str]:
    """Split a line into ``(words, uses_heredoc)`` commands; on failure return the reason."""
    tokens = _tokens(line)
    if tokens is None:
        return None, "command cannot be parsed"
    if "`" in line or "$(" in line or any(t in ("(", ")") for t in tokens):
        return None, "command substitution and subshells are not allowed"
    commands: list[tuple[list[str], bool]] = []
    words: list[str] = []
    heredoc = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in SEPARATORS:
            commands.append((words, heredoc))
            words, heredoc = [], False
        elif token == "&":
            return None, "background execution is not allowed"
        elif token in (">", "&>", ">&") and (
            (token == ">&" and following.isdigit()) or following == "/dev/null"
        ):
            if words and words[-1].isdigit():
                words.pop()
            index += 1
        elif token.startswith(">") or token.startswith("&>") or token.startswith("<<<"):
            return None, "redirection to a file is not allowed"
        elif token.startswith("<<"):
            heredoc = True
            index += 1
        elif token.startswith("<"):
            return None, "input redirection is not allowed"
        else:
            words.append(token)
        index += 1
    commands.append((words, heredoc))
    return [c for c in commands if c[0]], ""


def _check_command(words: list[str], heredoc: bool, cwd: str | None) -> Decision:
    program = words[0]
    if "/" in program or "\\" in program or "=" in program:
        return _deny(f"{program!r} must be invoked by its plain name")
    args = words[1:]
    if heredoc and not (program == "review-ctx" and args[:1] == ["emit"]):
        return _deny("heredoc input is only allowed for review-ctx emit")
    if program == "review-ctx":
        if not args or args[0] not in REVIEW_CTX_COMMANDS:
            allowed = ", ".join(sorted(REVIEW_CTX_COMMANDS))
            return _deny(f"review-ctx subcommand not allowed; use one of: {allowed}")
        if any(a in REVIEW_CTX_DENIED_FLAGS for a in args):
            return _deny("review-ctx option not allowed")
        return ALLOWED
    if program == "git":
        if not args or args[0] not in GIT_READ_COMMANDS:
            return _deny("only read-only git commands are allowed")
        if any(a.split("=")[0] in GIT_DENIED_FLAGS for a in args):
            return _deny("git option not allowed")
        for arg in args[1:]:
            problem = None if arg.startswith("-") else _check_path(arg.split(":")[-1], cwd)
            if problem:
                return problem
        return ALLOWED
    if program in TEXT_FILTERS:
        for arg in args:
            problem = None if arg.startswith("-") else _check_path(arg, cwd)
            if problem:
                return problem
        return ALLOWED
    return _deny(f"command {program!r} is not allowed for review agents")


def _bash(tool_input: dict[str, Any], cwd: str | None) -> Decision:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _deny("empty command")
    lines = _visible_lines(command)
    if lines is None:
        return _deny("heredoc is not terminated")
    for line in lines:
        commands, reason = _commands(line)
        if commands is None:
            return _deny(reason)
        for words, heredoc in commands:
            decision = _check_command(words, heredoc, cwd)
            if not decision.allowed:
                return decision
    return ALLOWED


def decide(event: dict[str, Any]) -> Decision | None:
    """Decision for a ``PreToolUse`` event, or ``None`` when it is not from a review agent."""
    if not is_guarded(event.get("agent_type")):
        return None
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd")
    if tool == "Bash":
        return _bash(tool_input, cwd)
    if tool in READ_TOOLS:
        return _read_tool(tool, tool_input, cwd)
    return _deny(f"tool {tool!r} is not available to review agents")


def hook_output(decision: Decision) -> str:
    """The JSON a ``PreToolUse`` hook prints to allow or deny a call."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if decision.allowed else "deny",
            "permissionDecisionReason": decision.reason,
        }
    }
    return json.dumps(payload)
