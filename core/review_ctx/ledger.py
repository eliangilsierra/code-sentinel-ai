"""Append-only ledger of findings and verdicts with mechanical validation of citations."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_ctx.schemas import validation_errors

FINDINGS_FILE = "findings.jsonl"
VERDICTS_FILE = "verdicts.jsonl"
REJECTIONS_FILE = "rejections.jsonl"
CITATION_WINDOW = 3
SUGGESTION_WINDOW = 10
MIN_SUGGESTION_RATIO = 0.5
MAX_ATTEMPTS = 3
FINGERPRINT_LENGTH = 10
LINE_BUCKET = 10
MAX_NOTE_LENGTH = 120
VERDICTS = ("confirmed", "refuted", "unverifiable")
SEVERITIES = ("important", "nit", "pre_existing", "question")
_WHITESPACE = re.compile(r"\s+")


class LedgerError(Exception):
    """Raised for unusable ledger operations such as an unknown finding id."""


@dataclass
class EmitResult:
    """Outcome of an emit: accepted (possibly repeated) or rejected with actionable messages."""

    status: str
    id: str | None = None
    line_corrected: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")


def normalize(text: str) -> str:
    """Collapse whitespace runs and trim the ends."""
    return _WHITESPACE.sub(" ", text).strip()


def fingerprint(finding: dict[str, Any]) -> str:
    """Stable id of a finding: lens, file, normalized claim and the ten-line window of its start."""
    bucket = (finding["loc"]["l"][0] - 1) // LINE_BUCKET
    material = "|".join(
        [finding["lens"], finding["loc"]["f"], normalize(finding["claim"]).lower(), str(bucket)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


class Ledger:
    """The findings, verdicts and rejections of one run, validated against the working tree."""

    def __init__(self, run_dir: Path, root: Path) -> None:
        self.run_dir = run_dir
        self.root = root.resolve()

    def emit(self, raw: str) -> EmitResult:
        """Validate and record one finding given as JSON text."""
        try:
            finding = json.loads(raw)
        except json.JSONDecodeError as error:
            return EmitResult("rejected", errors=[f"input is not valid JSON: {error}"])
        errors = validation_errors("finding", finding)
        if errors:
            return EmitResult("rejected", errors=errors)
        errors, corrected = self._validate_citations(finding)
        key = fingerprint(finding)
        if errors:
            return self._reject(key, finding, errors)
        if key in self._ids():
            return EmitResult("duplicate", id=key, line_corrected=corrected)
        self._append(FINDINGS_FILE, _record(key, finding, corrected, "candidate"))
        return EmitResult("accepted", id=key, line_corrected=corrected)

    def findings(self) -> list[dict[str, Any]]:
        """Every recorded finding, in emission order."""
        return self._read(FINDINGS_FILE)

    def verdicts(self) -> list[dict[str, Any]]:
        """Every recorded verdict, in the order given."""
        return self._read(VERDICTS_FILE)

    def get(self, finding_id: str) -> dict[str, Any]:
        """Return the record of ``finding_id``."""
        for record in self.findings():
            if record["id"] == finding_id:
                return record
        raise LedgerError(f"unknown finding id {finding_id!r}")

    def add_verdict(
        self, finding_id: str, verdict: str, note: str, severity: str | None = None
    ) -> dict[str, Any]:
        """Record a verifier verdict, optionally adjusting the severity."""
        if verdict not in VERDICTS:
            raise LedgerError(f"verdict must be one of {', '.join(VERDICTS)}")
        if severity is not None and severity not in SEVERITIES:
            raise LedgerError(f"severity must be one of {', '.join(SEVERITIES)}")
        if len(note) > MAX_NOTE_LENGTH:
            raise LedgerError(f"note has {len(note)} characters; the limit is {MAX_NOTE_LENGTH}")
        self.get(finding_id)
        entry: dict[str, Any] = {"id": finding_id, "verdict": verdict, "note": note}
        if severity is not None:
            entry["sev"] = severity
        self._append(VERDICTS_FILE, entry)
        return entry

    def _validate_citations(self, finding: dict[str, Any]) -> tuple[list[str], bool]:
        errors: list[str] = []
        corrected = False
        loc = finding["loc"]
        lines, problem = self._lines(loc["f"])
        if problem:
            errors.append(f"loc.f: {problem}")
        elif loc["l"][0] > loc["l"][1]:
            errors.append(f"loc.l: start {loc['l'][0]} is greater than end {loc['l'][1]}")
        elif loc["l"][1] > len(lines):
            errors.append(f"loc.l: {loc['f']} has {len(lines)} lines but loc ends at {loc['l'][1]}")
        for index, item in enumerate(finding["trace"]):
            lines, problem = self._lines(item["f"])
            if problem:
                errors.append(f"trace[{index}].f: {problem}")
                continue
            line = _locate(item["q"], item["l"], lines)
            if line is None:
                errors.append(_missing(index, item, lines))
            elif line != item["l"]:
                item["l"] = line
                corrected = True
        return errors, corrected

    def _lines(self, path: str) -> tuple[list[str], str | None]:
        candidate = (self.root / path).resolve()
        if Path(path).is_absolute() or not candidate.is_relative_to(self.root):
            return [], f"{path!r} must be a path inside the repository"
        if not candidate.is_file():
            return [], f"{path!r} does not exist in the working tree"
        try:
            return candidate.read_text(encoding="utf-8", errors="replace").splitlines(), None
        except OSError as error:
            return [], f"cannot read {path!r}: {error}"

    def _reject(self, key: str, finding: dict[str, Any], errors: list[str]) -> EmitResult:
        attempts = sum(1 for entry in self._read(REJECTIONS_FILE) if entry["fingerprint"] == key)
        self._append(REJECTIONS_FILE, {"fingerprint": key, "errors": errors})
        if attempts + 1 < MAX_ATTEMPTS:
            return EmitResult("rejected", errors=errors)
        if key not in self._ids():
            self._append(FINDINGS_FILE, _record(key, finding, False, "invalid_citation"))
        message = f"recorded as invalid_citation after {MAX_ATTEMPTS} attempts; do not retry"
        return EmitResult("abandoned", id=key, errors=[*errors, message])

    def _ids(self) -> set[str]:
        return {record["id"] for record in self.findings()}

    def _read(self, name: str) -> list[dict[str, Any]]:
        path = self.run_dir / name
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def _append(self, name: str, entry: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / name).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _record(key: str, finding: dict[str, Any], corrected: bool, status: str) -> dict[str, Any]:
    return {"id": key, "status": status, "line_corrected": corrected, "finding": finding}


def _locate(quote: str, line: int, lines: list[str]) -> int | None:
    """Line number where ``quote`` appears, preferring ``line`` and then its nearest neighbours."""
    wanted = normalize(quote)
    offsets = sorted(range(-CITATION_WINDOW, CITATION_WINDOW + 1), key=abs)
    for offset in offsets:
        number = line + offset
        if 1 <= number <= len(lines) and wanted in normalize(lines[number - 1]):
            return number
    return None


def _missing(index: int, item: dict[str, Any], lines: list[str]) -> str:
    message = f"trace[{index}]: quote not found near {item['f']}:{item['l']}"
    wanted = normalize(item["q"])
    start = max(1, item["l"] - SUGGESTION_WINDOW)
    end = min(len(lines), item["l"] + SUGGESTION_WINDOW)
    best: tuple[float, int] = (0.0, 0)
    for number in range(start, end + 1):
        ratio = difflib.SequenceMatcher(None, wanted, normalize(lines[number - 1])).ratio()
        best = max(best, (ratio, number))
    if best[0] >= MIN_SUGGESTION_RATIO:
        return f"{message}; closest line {best[1]}: {normalize(lines[best[1] - 1])!r}"
    return message
