"""Rendering of the gate result as terminal text, Markdown or JSON."""

from __future__ import annotations

import json
from typing import Any

from review_ctx.gate import Decision, GateResult
from review_ctx.redact import redact, redact_data

FORMATS = ("terminal", "markdown", "json")
SECTIONS = (("important", "Important"), ("pre_existing", "Pre-existing"), ("nit", "Nit"))


def render(result: GateResult, fmt: str) -> str:
    """Render ``result`` in ``fmt``; the text is redacted of credential-like strings."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if fmt == "json":
        data = redact_data(_as_json(result))
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return redact(_markdown(result) if fmt == "markdown" else _terminal(result))


def _summary(result: GateResult) -> str:
    published = result.published
    parts = [
        f"{sum(1 for d in published if d.sev == sev)} {label.lower()}" for sev, label in SECTIONS
    ]
    return ", ".join(parts)


def _footer(result: GateResult) -> str:
    counts = result.counts()
    return (
        f"held as questions: {counts['degrade'] + counts['hold']}; "
        f"discarded: {counts['drop']}; nits over the cap: {result.nits_suppressed}"
    )


def _location(decision: Decision) -> str:
    loc = decision.finding["loc"]
    start, end = loc["l"]
    return f"{loc['f']}:{start}" if start == end else f"{loc['f']}:{start}-{end}"


def _evidence(decision: Decision) -> list[str]:
    return [f"{t['f']}:{t['l']}  {t['q']}" for t in decision.finding["trace"]]


def _terminal(result: GateResult) -> str:
    lines = [f"code-sentinel: {_summary(result)}", f"({_footer(result)})", ""]
    for severity, label in SECTIONS:
        for decision in (d for d in result.published if d.sev == severity):
            finding = decision.finding
            lines.append(
                f"{label.upper()}  {_location(decision)}  [{finding['lens']}, {decision.tier}]"
            )
            lines.append(f"  {finding['claim']}")
            lines.append(f"  trigger: {finding['trigger']}")
            lines += [f"  evidence: {item}" for item in _evidence(decision)]
            if finding.get("fix"):
                lines.append(f"  fix: {finding['fix']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _markdown(result: GateResult) -> str:
    lines = [f"# Review: {_summary(result)}", "", f"_{_footer(result)}_", ""]
    for severity, label in SECTIONS:
        group = [d for d in result.published if d.sev == severity]
        if not group:
            continue
        lines += [f"## {label} ({len(group)})", ""]
        for decision in group:
            finding = decision.finding
            lines.append(f"### `{_location(decision)}` {finding['lens']} ({decision.tier})")
            lines += ["", finding["claim"], "", f"**Trigger:** {finding['trigger']}", ""]
            lines += [f"- `{item}`" for item in _evidence(decision)]
            if finding.get("fix"):
                lines += ["", f"**Fix:** {finding['fix']}"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _entry(decision: Decision) -> dict[str, Any]:
    finding = decision.finding
    entry = {
        "id": decision.id,
        "lens": finding["lens"],
        "sev": decision.sev,
        "tier": decision.tier,
        "loc": finding["loc"],
        "claim": finding["claim"],
        "trigger": finding["trigger"],
        "trace": finding["trace"],
    }
    if finding.get("fix"):
        entry["fix"] = finding["fix"]
    return entry


def _as_json(result: GateResult) -> dict[str, Any]:
    return {
        "published": [_entry(d) for d in result.published],
        "questions": [_entry(d) for d in result.decisions if d.action in ("degrade", "hold")],
        "suppressed": [{**_entry(d), "reason": d.reason} for d in result.with_action("drop")],
        "counts": result.counts(),
    }
