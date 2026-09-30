"""Static validation of evaluation case files."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from review_ctx.schemas import validation_errors

LineCounter = Callable[[str], int | None]


@dataclass(frozen=True)
class LintIssue:
    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def lint_case(data: Any, line_count: LineCounter | None = None) -> list[str]:
    """Return the rule violations of one parsed case.

    ``line_count`` maps a repository path to its number of lines at the case head, or ``None``
    when the file does not exist there. When omitted, locations are not checked against files.
    """
    errors = validation_errors("case", data)
    if errors:
        return errors
    issues: list[str] = []
    issues += _check_category(data)
    issues += _check_refs(data)
    issues += _check_locations(data, line_count)
    return issues


def _check_category(case: dict[str, Any]) -> list[str]:
    category = case["category"]
    expected = case["expected"]
    issues: list[str] = []
    if category == "negative" and expected:
        issues.append("category 'negative' requires an empty 'expected' list")
    if category == "bug" and not expected:
        issues.append("category 'bug' requires at least one expected finding")
    if category == "pre_existing" and not any(e["sev"] == "pre_existing" for e in expected):
        issues.append(
            "category 'pre_existing' requires an expected finding with sev 'pre_existing'"
        )
    if case["adversarial"] != (category == "adversarial"):
        issues.append("'adversarial' must be true exactly when category is 'adversarial'")
    return issues


def _check_refs(case: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if case["base"] == case["head"]:
        issues.append("'base' and 'head' must differ")
    for index, item in enumerate(case["expected"]):
        if item["loc"]["l"][0] > item["loc"]["l"][1]:
            issues.append(f"expected[{index}].loc.l start is greater than its end")
    for index, item in enumerate(case.get("optional", [])):
        if item["loc"]["l"][0] > item["loc"]["l"][1]:
            issues.append(f"optional[{index}].loc.l start is greater than its end")
    return issues


def _check_locations(case: dict[str, Any], line_count: LineCounter | None) -> list[str]:
    if line_count is None:
        return []
    issues: list[str] = []
    located = [(f"expected[{i}]", e["loc"]) for i, e in enumerate(case["expected"])]
    located += [(f"optional[{i}]", o["loc"]) for i, o in enumerate(case.get("optional", []))]
    for label, loc in located:
        total = line_count(loc["f"])
        if total is None:
            issues.append(f"{label}.loc.f '{loc['f']}' does not exist at head")
        elif loc["l"][1] > total:
            issues.append(f"{label}.loc.l ends at {loc['l'][1]} but '{loc['f']}' has {total} lines")
    for index, item in enumerate(case["expected"]):
        for cite in item.get("must_cite", []):
            path, _, line = cite.rpartition(":")
            total = line_count(path)
            if total is None:
                issues.append(f"expected[{index}].must_cite '{cite}' does not exist at head")
            elif int(line) > total:
                issues.append(f"expected[{index}].must_cite '{cite}' is beyond line {total}")
    for index, item in enumerate(case["must_not"]):
        if "f" in item and line_count(item["f"]) is None:
            issues.append(f"must_not[{index}].f '{item['f']}' does not exist at head")
    return issues


def collect_case_files(paths: Iterable[Path]) -> list[Path]:
    """Expand directories into their ``*.yaml`` files, keeping explicit files as given."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        else:
            files.append(path)
    return files


def lint_files(
    files: Iterable[Path],
    line_counter_for: Callable[[dict[str, Any]], LineCounter] | None = None,
) -> list[LintIssue]:
    """Lint case files, including cross-file rules (unique ids, id equal to file name)."""
    issues: list[LintIssue] = []
    ids: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        data, problem = _load(path)
        if problem is not None:
            issues.append(LintIssue(path, problem))
            continue
        counter = line_counter_for(data) if line_counter_for and _is_plain_case(data) else None
        issues += [LintIssue(path, message) for message in lint_case(data, counter)]
        case_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(case_id, str):
            ids[case_id].append(path)
            if case_id != path.stem:
                issues.append(LintIssue(path, f"id '{case_id}' must match the file name"))
    for case_id, paths in ids.items():
        if len(paths) > 1:
            listed = ", ".join(str(p) for p in paths)
            issues.append(LintIssue(paths[0], f"duplicate id '{case_id}' in {listed}"))
    return issues


def _is_plain_case(data: Any) -> bool:
    return isinstance(data, dict) and not validation_errors("case", data)


def _load(path: Path) -> tuple[Any, str | None]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, f"cannot read case: {error}"
    if not isinstance(data, dict):
        return None, "case must be a mapping"
    return data, None
