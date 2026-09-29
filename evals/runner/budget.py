"""Spending ledger that keeps evaluation runs within a fixed budget."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_LEDGER = Path("evals/reports/budget.json")


class BudgetError(Exception):
    """Raised when the ledger is missing, malformed or would be exceeded."""


@dataclass(frozen=True)
class Entry:
    run: str
    usd: float
    at: str


@dataclass
class Budget:
    """A spending limit in API-equivalent dollars and the runs charged against it."""

    limit_usd: float
    entries: list[Entry] = field(default_factory=list)

    @property
    def spent_usd(self) -> float:
        return sum(entry.usd for entry in self.entries)

    @property
    def remaining_usd(self) -> float:
        return self.limit_usd - self.spent_usd

    def record(self, run: str, usd: float, at: datetime | None = None) -> Entry:
        """Charge ``usd`` to the budget under the run id ``run``."""
        if usd < 0:
            raise BudgetError("a run cannot cost a negative amount")
        stamp = (at or datetime.now(UTC)).isoformat(timespec="seconds")
        entry = Entry(run=run, usd=usd, at=stamp)
        self.entries.append(entry)
        return entry

    def projection(self, cases: int, mean_cost_usd: float, margin: float = 0.0) -> float:
        """Expected cost of ``cases`` more cases at ``mean_cost_usd`` each, plus ``margin``."""
        return cases * mean_cost_usd * (1 + margin)

    def ensure_affordable(self, cases: int, mean_cost_usd: float, margin: float = 0.0) -> float:
        """Return the projected cost, or raise if it exceeds the remaining budget."""
        projected = self.projection(cases, mean_cost_usd, margin)
        if projected > self.remaining_usd:
            raise BudgetError(
                f"projected ${projected:.2f} exceeds the remaining ${self.remaining_usd:.2f}"
            )
        return projected

    @classmethod
    def load(cls, path: Path = DEFAULT_LEDGER) -> Budget:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [Entry(e["run"], float(e["usd"]), e["at"]) for e in data["entries"]]
            return cls(limit_usd=float(data["limit_usd"]), entries=entries)
        except FileNotFoundError as error:
            raise BudgetError(f"budget ledger not found: {path}") from error
        except (KeyError, TypeError, ValueError) as error:
            raise BudgetError(f"budget ledger {path} is malformed: {error}") from error

    def save(self, path: Path = DEFAULT_LEDGER) -> None:
        """Write the ledger atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "limit_usd": self.limit_usd,
            "entries": [{"run": e.run, "usd": e.usd, "at": e.at} for e in self.entries],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
