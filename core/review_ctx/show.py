"""Text rendering of a finding with its citations and surrounding code, for the verifier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from review_ctx.ledger import Ledger

MAX_CODE_CHARS = 6000
CONTEXT_LINES = 5


def render_finding(ledger: Ledger, finding_id: str) -> str:
    """Return the finding ``finding_id`` with its trace, verdicts and the hunk it touches."""
    record = ledger.get(finding_id)
    finding = record["finding"]
    loc = finding["loc"]
    lines = [
        f"id: {record['id']}  status: {record['status']}",
        f"lens: {finding['lens']}  sev: {finding['sev']}",
        f"loc: {loc['f']}:{loc['l'][0]}-{loc['l'][1]}",
        f"claim: {finding['claim']}",
        f"trigger: {finding['trigger']}",
    ]
    if "fix" in finding:
        lines.append(f"fix: {finding['fix']}")
    if "src" in finding:
        lines.append(f"src: {finding['src']}")
    lines.append("trace:")
    for item in finding["trace"]:
        why = f"  ({item['why']})" if item.get("why") else ""
        lines.append(f"  {item['f']}:{item['l']}  {item['q']}{why}")
    for verdict in (v for v in ledger.verdicts() if v["id"] == finding_id):
        adjusted = f" sev={verdict['sev']}" if "sev" in verdict else ""
        lines.append(f"verdict: {verdict['verdict']}{adjusted}  {verdict['note']}")
    lines.append(_code_block(ledger, loc))
    return "\n".join(lines)


def _code_block(ledger: Ledger, loc: dict[str, Any]) -> str:
    hunk = _packet_hunk(ledger.run_dir, loc)
    if hunk is not None:
        header = f"hunk {hunk['id']} scope: {hunk['scope']} lines {hunk['l'][0]}-{hunk['l'][1]}"
        return f"{header}\n{_truncate(hunk['code'])}"
    path = ledger.root / loc["f"]
    start = max(1, loc["l"][0] - CONTEXT_LINES)
    end = loc["l"][1] + CONTEXT_LINES
    numbered = [
        f"{number}: {text}"
        for number, text in enumerate(path.read_text("utf-8", "replace").splitlines(), 1)
        if start <= number <= end
    ]
    return f"code {loc['f']} lines {start}-{end}\n{_truncate(chr(10).join(numbered))}"


def _packet_hunk(run_dir: Path, loc: dict[str, Any]) -> dict[str, Any] | None:
    packet = run_dir / "packet.json"
    if not packet.is_file():
        return None
    for hunk in json.loads(packet.read_text(encoding="utf-8"))["hunks"]:
        overlaps = hunk["l"][0] <= loc["l"][1] and loc["l"][0] <= hunk["l"][1]
        if hunk["p"] == loc["f"] and overlaps:
            return hunk
    return None


def _truncate(code: str) -> str:
    if len(code) <= MAX_CODE_CHARS:
        return code
    return code[:MAX_CODE_CHARS] + "\n... truncated"
