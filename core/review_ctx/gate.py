"""Deterministic publication gate: evidence tiers, severity policy, deduplication and caps."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from review_ctx.ledger import Ledger

GATE_FILE = "gate.json"
TIERS = ("E3", "E2", "E1", "E0")
SEVERITIES = ("important", "nit", "pre_existing", "question")
ACTIONS = ("publish", "publish_if_unverified", "degrade", "hold", "drop")
SEVERITY_ORDER = {"important": 0, "pre_existing": 1, "nit": 2, "question": 3}


class PolicyError(Exception):
    """Raised when a gate policy is malformed."""


@dataclass(frozen=True)
class Policy:
    max_nits: int
    tools: frozenset[str]
    trusted_tools: frozenset[str]
    severity_rules: dict[str, dict[str, str]]
    blocked_cells: frozenset[tuple[str, str]]
    ignore_paths: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        rules = data.get("severity_rules")
        if not isinstance(rules, dict):
            raise PolicyError("severity_rules must be a mapping")
        for severity in SEVERITIES:
            for tier in TIERS:
                action = (rules.get(severity) or {}).get(tier)
                if action not in ACTIONS:
                    raise PolicyError(f"severity_rules.{severity}.{tier} must be one of {ACTIONS}")
        max_nits = data.get("max_nits")
        if not isinstance(max_nits, int) or max_nits < 0:
            raise PolicyError("max_nits must be a non-negative integer")
        blocked = frozenset(
            (cell["lens"], cell["tier"]) for cell in data.get("blocked_cells") or []
        )
        return cls(
            max_nits=max_nits,
            tools=frozenset(data.get("tools") or []),
            trusted_tools=frozenset(data.get("trusted_tools") or []),
            severity_rules=rules,
            blocked_cells=blocked,
            ignore_paths=tuple(data.get("ignore_paths") or []),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        """Load the policy at ``path``, or the one shipped with the package."""
        try:
            if path is None:
                text = resources.files("review_ctx").joinpath("gate_policy.yaml").read_text("utf-8")
            else:
                text = path.read_text(encoding="utf-8")
            return cls.from_dict(yaml.safe_load(text))
        except (OSError, yaml.YAMLError, KeyError, TypeError) as error:
            raise PolicyError(f"cannot load gate policy: {error}") from error


@dataclass(frozen=True)
class Decision:
    """What the gate decided for one finding, and why."""

    id: str
    action: str
    tier: str
    sev: str
    reason: str
    verified: bool
    finding: dict[str, Any]


@dataclass
class GateResult:
    decisions: list[Decision] = field(default_factory=list)
    nits_suppressed: int = 0

    def with_action(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]

    @property
    def published(self) -> list[Decision]:
        return self.with_action("publish")

    def counts(self) -> dict[str, int]:
        counts = {
            action: len(self.with_action(action))
            for action in ACTIONS
            if action != "publish_if_unverified"
        }
        counts["nits_suppressed"] = self.nits_suppressed
        return counts


def tool_of(finding: dict[str, Any]) -> str | None:
    """Name of the tool that produced the finding, taken from its ``src`` field."""
    source = finding.get("src")
    return source.split(":", 1)[0] if source else None


def aggregate_verdict(verdicts: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Combine the verdicts of one finding into one verdict and an optional severity.

    Any refutation wins, then any unverifiable verdict; the finding is confirmed only when every
    verdict confirms it. The severity is the last adjustment given.
    """
    if not verdicts:
        return None, None
    kinds = {v["verdict"] for v in verdicts}
    combined = (
        "refuted"
        if "refuted" in kinds
        else "unverifiable"
        if "unverifiable" in kinds
        else "confirmed"
    )
    adjusted = [v["sev"] for v in verdicts if "sev" in v]
    return combined, adjusted[-1] if adjusted else None


def run_gate(ledger: Ledger, policy: Policy | None = None) -> GateResult:
    """Decide the fate of every finding of the run and persist ``gate.json``."""
    policy = policy or Policy.load()
    verdicts: dict[str, list[dict[str, Any]]] = {}
    for verdict in ledger.verdicts():
        verdicts.setdefault(verdict["id"], []).append(verdict)
    decisions = [
        _decide(record, verdicts.get(record["id"], []), policy) for record in ledger.findings()
    ]
    decisions = _deduplicate(decisions)
    result = GateResult(*_cap_nits(decisions, policy.max_nits))
    result.decisions = sorted(result.decisions, key=_order)
    _persist(ledger.run_dir, result)
    return result


def _decide(record: dict[str, Any], verdicts: list[dict[str, Any]], policy: Policy) -> Decision:
    finding = record["finding"]
    verdict, adjusted = aggregate_verdict(verdicts)
    severity = adjusted or finding["sev"]
    tool = tool_of(finding)
    verified = verdict in ("confirmed", "unverifiable")

    def decision(action: str, tier: str, reason: str) -> Decision:
        return Decision(record["id"], action, tier, severity, reason, verified, finding)

    if record["status"] == "invalid_citation":
        return decision("drop", "E0", "invalid_citation")
    if any(fnmatch.fnmatch(finding["loc"]["f"], pattern) for pattern in policy.ignore_paths):
        return decision("drop", "E0", "ignored_path")
    if verdict == "refuted":
        return decision("drop", "E0", "refuted")
    if verdict == "confirmed":
        tier = "E3" if tool in policy.tools else "E2"
    elif verdict is None and tool in policy.trusted_tools:
        tier = "E3"
    else:
        tier = "E1"
    action = policy.severity_rules[severity][tier]
    reason = f"{severity}/{tier}"
    if action == "publish_if_unverified":
        action, reason = ("drop", "unverifiable") if verified else ("publish", "unverified")
    if action == "publish" and (finding["lens"], tier) in policy.blocked_cells:
        return decision("drop", tier, "cell_blocked")
    if action == "degrade":
        reason = "unverifiable important degraded to question"
    return decision(action, tier, reason)


def _rank(decision: Decision) -> tuple[int, int, str, int]:
    loc = decision.finding["loc"]
    return (TIERS.index(decision.tier), SEVERITY_ORDER[decision.sev], loc["f"], loc["l"][0])


def _order(decision: Decision) -> tuple[int, int, str, int]:
    loc = decision.finding["loc"]
    return (SEVERITY_ORDER[decision.sev], TIERS.index(decision.tier), loc["f"], loc["l"][0])


def _overlaps(first: dict[str, Any], second: dict[str, Any]) -> bool:
    a, b = first["loc"], second["loc"]
    return a["f"] == b["f"] and a["l"][0] <= b["l"][1] and b["l"][0] <= a["l"][1]


def _deduplicate(decisions: list[Decision]) -> list[Decision]:
    """Keep the best decision among findings of one lens that cover overlapping lines."""
    kept: list[Decision] = []
    result: dict[str, Decision] = {}
    for decision in sorted(decisions, key=_rank):
        if decision.action == "drop":
            result[decision.id] = decision
            continue
        twin = next(
            (
                k
                for k in kept
                if k.finding["lens"] == decision.finding["lens"]
                and _overlaps(k.finding, decision.finding)
            ),
            None,
        )
        if twin is None:
            kept.append(decision)
            result[decision.id] = decision
        else:
            result[decision.id] = Decision(
                decision.id,
                "drop",
                decision.tier,
                decision.sev,
                f"duplicate_of:{twin.id}",
                decision.verified,
                decision.finding,
            )
    return [result[d.id] for d in decisions]


def _cap_nits(decisions: list[Decision], limit: int) -> tuple[list[Decision], int]:
    published_nits = sorted(
        (d for d in decisions if d.action == "publish" and d.sev == "nit"), key=_rank
    )
    excess = {d.id for d in published_nits[limit:]}
    capped = [
        Decision(d.id, "drop", d.tier, d.sev, "nit_cap", d.verified, d.finding)
        if d.id in excess
        else d
        for d in decisions
    ]
    return capped, len(excess)


def _persist(run_dir: Path, result: GateResult) -> None:
    payload = {
        "decisions": [asdict(d) for d in result.decisions],
        "nits_suppressed": result.nits_suppressed,
        "counts": result.counts(),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / GATE_FILE).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
