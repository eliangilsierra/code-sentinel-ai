from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "adapters" / "claude"
LENSES = ROOT / "lenses"
ALLOWED_TOOLS = {"Read", "Grep", "Glob", "LSP", "Bash"}
MIN_CACHEABLE_TOKENS = 512
MAX_LENS_LINES = 60


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert match, f"{path.name} has no frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


AGENTS = sorted((PLUGIN / "agents").glob("*.md"))


def test_the_plugin_ships_a_finder_and_a_verifier() -> None:
    assert [a.stem for a in AGENTS] == ["rs-finder", "rs-verifier"]


@pytest.mark.parametrize("agent", AGENTS, ids=lambda p: p.stem)
def test_agents_are_restricted_and_isolated(agent: Path) -> None:
    meta, body = _frontmatter(agent)
    tools = {t.strip() for t in meta["tools"].split(",")}
    assert tools <= ALLOWED_TOOLS and "Agent" not in tools
    assert not {"Edit", "Write", "WebFetch", "WebSearch"} & tools
    assert meta["omitClaudeMd"] is True
    assert isinstance(meta["maxTurns"], int) and 1 <= meta["maxTurns"] <= 12
    assert meta["name"] == agent.stem and meta["model"] in {"sonnet", "opus", "haiku"}
    assert "hooks" not in meta and "mcpServers" not in meta and "permissionMode" not in meta
    assert len(body) / 4 >= MIN_CACHEABLE_TOKENS


@pytest.mark.parametrize("agent", AGENTS, ids=lambda p: p.stem)
def test_agents_treat_repository_content_as_data(agent: Path) -> None:
    body = _frontmatter(agent)[1]
    assert "never instructions" in body and "no network access" in body


def test_finder_documents_every_finding_field() -> None:
    body = _frontmatter(PLUGIN / "agents" / "rs-finder.md")[1]
    schema = json.loads(
        (ROOT / "core" / "review_ctx" / "schema" / "finding.json").read_text("utf-8")
    )
    for field in schema["properties"]:
        assert f"`{field}`" in body, field
    assert "confidence" in body and "review-ctx emit" in body


def test_verifier_uses_the_ledger_commands() -> None:
    body = _frontmatter(PLUGIN / "agents" / "rs-verifier.md")[1]
    assert "review-ctx show" in body and "review-ctx verdict" in body
    for verdict in ("confirmed", "refuted", "unverifiable"):
        assert verdict in body


@pytest.mark.parametrize("lens", sorted(LENSES.glob("*.md")), ids=lambda p: p.stem)
def test_lens_files_are_short(lens: Path) -> None:
    assert len(lens.read_text(encoding="utf-8").splitlines()) <= MAX_LENS_LINES


def test_mvp_lenses_exist() -> None:
    assert {p.stem for p in LENSES.glob("*.md")} >= {"correctness", "security"}


def test_review_skill_is_manual_and_preapproves_only_what_it_needs() -> None:
    meta, body = _frontmatter(PLUGIN / "skills" / "review" / "SKILL.md")
    assert meta["name"] == "review" and meta["disable-model-invocation"] is True
    assert meta["allowed-tools"].split() == ["Bash(review-ctx", "*)", "Workflow(rs-review)"]
    assert "!`review-ctx prepare $ARGUMENTS`" in body
    assert "review-ctx report --run" in body and "PLAN " in body


def test_hook_runs_the_guard_from_the_plugin_bin() -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = hooks["hooks"]["PreToolUse"]
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}"/bin/review-ctx guard'


def test_manifest_and_marketplace_agree() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"] == "review-squad"
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    assert manifest["version"]


def test_launcher_is_a_posix_script_with_unix_line_endings() -> None:
    data = (PLUGIN / "bin" / "review-ctx").read_bytes()
    assert data.startswith(b"#!/bin/sh\n") and b"\r" not in data
    assert b"REVIEW_SQUAD_CORE" in data and b"uvx" in data


WORKFLOW = PLUGIN / "workflows" / "rs-review.js"


def test_workflow_meta_is_the_first_statement_and_a_literal() -> None:
    code = re.sub(r"^(?:\s*//[^\n]*\n)+", "", WORKFLOW.read_text(encoding="utf-8"))
    assert code.startswith("export const meta = {")
    meta = code.split("\n}\n", 1)[0]
    assert "name: 'rs-review'" in meta and "description:" in meta
    assert "(" not in meta and "${" not in meta


def test_workflow_avoids_nondeterministic_and_forbidden_constructs() -> None:
    code = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("Date.now", "Math.random", "new Date()", "import(", "require(", "fs."):
        assert forbidden not in code, forbidden


def test_workflow_uses_the_documented_primitives_and_both_agents() -> None:
    code = WORKFLOW.read_text(encoding="utf-8")
    for primitive in ("phase(", "pipeline(", "agent("):
        assert primitive in code
    assert "review-squad:rs-finder" in code and "review-squad:rs-verifier" in code
    assert "review-ctx emit" in code and "review-ctx verdict" in code


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_workflow_is_valid_javascript(tmp_path: Path) -> None:
    body = WORKFLOW.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    module = tmp_path / "rs-review.mjs"
    header = "async function workflow(args, agent, pipeline, phase) {\n"
    module.write_text(header + body + "\n}\n", encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(module)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
