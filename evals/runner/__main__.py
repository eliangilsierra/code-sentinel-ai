"""Command-line entry point: python -m evals.runner <command>."""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

from evals.runner.budget import DEFAULT_LEDGER, Budget, BudgetError
from evals.runner.fixtures import FixtureError, FixtureIndex, bundle_path, materialize
from evals.runner.lint import collect_case_files, lint_files
from evals.runner.report import (
    FAIL,
    compare,
    load_summary,
    regression_verdict,
    render_comparison,
    render_run,
)

COMMANDS = ("run", "judge", "report", "calibrate", "case-lint", "materialize", "budget")
NOT_IMPLEMENTED = 3
DEFAULT_CASES_DIR = Path("evals/cases")
DEFAULT_FIXTURES_DIR = Path("evals/fixtures")


def _case_lint(args: argparse.Namespace) -> int:
    paths = args.paths or [DEFAULT_CASES_DIR]
    missing = [path for path in paths if not path.exists()]
    if missing:
        print(f"case-lint: path not found: {missing[0]}", file=sys.stderr)
        return 2
    files = collect_case_files(paths)
    if args.fixtures_dir is None:
        issues = lint_files(files)
    else:
        with FixtureIndex(args.fixtures_dir) as index:
            issues = lint_files(files, index.line_counter_for)
    for issue in issues:
        print(issue)
    print(f"case-lint: {len(files)} case(s) checked, {len(issues)} issue(s)")
    return 1 if issues else 0


def _budget(args: argparse.Namespace) -> int:
    try:
        if args.init is not None:
            if args.file.exists():
                print(f"budget: ledger already exists: {args.file}", file=sys.stderr)
                return 2
            Budget(limit_usd=args.init).save(args.file)
        budget = Budget.load(args.file)
        if args.record is not None:
            budget.record(args.record[0], float(args.record[1]))
            budget.save(args.file)
    except (BudgetError, ValueError) as error:
        print(f"budget: {error}", file=sys.stderr)
        return 2
    print(
        f"limit ${budget.limit_usd:.2f}  spent ${budget.spent_usd:.2f}  "
        f"remaining ${budget.remaining_usd:.2f}"
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    try:
        current = load_summary(args.run)
        reference = load_summary(args.compare) if args.compare else None
    except ValueError as error:
        print(f"report: {error}", file=sys.stderr)
        return 2
    print(render_run(current))
    if reference is None:
        return 0
    comparison = compare(current, reference)
    level, reasons = regression_verdict(comparison)
    print(render_comparison(comparison, level, reasons))
    return 1 if level == FAIL else 0


def _find_case(reference: str, cases_dir: Path) -> Path | None:
    candidate = Path(reference)
    if candidate.is_file():
        return candidate
    matches = sorted(cases_dir.rglob(f"{reference}.yaml")) if cases_dir.is_dir() else []
    return matches[0] if matches else None


def _materialize(args: argparse.Namespace) -> int:
    case_file = _find_case(args.case, args.cases_dir)
    if case_file is None:
        print(f"materialize: case not found: {args.case}", file=sys.stderr)
        return 2
    case = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    dest = args.dest or Path(tempfile.mkdtemp(prefix="review-squad-")) / "repo"
    try:
        materialize(
            bundle_path(args.fixtures_dir, case["fixture"]), case["base"], case["head"], dest
        )
    except FixtureError as error:
        print(f"materialize: {error}", file=sys.stderr)
        return 2
    print(dest)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = subcommands.add_parser(name)
        if name == "case-lint":
            sub.add_argument("paths", nargs="*", type=Path)
            sub.add_argument("--fixtures-dir", type=Path, default=None)
            sub.set_defaults(handler=_case_lint)
        elif name == "report":
            sub.add_argument("run", type=Path)
            sub.add_argument("--compare", type=Path, default=None)
            sub.set_defaults(handler=_report)
        elif name == "budget":
            sub.add_argument("--file", type=Path, default=DEFAULT_LEDGER)
            sub.add_argument("--init", type=float, default=None, metavar="LIMIT_USD")
            sub.add_argument("--record", nargs=2, default=None, metavar=("RUN", "USD"))
            sub.set_defaults(handler=_budget)
        elif name == "materialize":
            sub.add_argument("case")
            sub.add_argument("--dest", type=Path, default=None)
            sub.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR)
            sub.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
            sub.set_defaults(handler=_materialize)
        else:
            sub.add_argument("options", nargs="*")
    args, _ = parser.parse_known_args(argv)
    handler = getattr(args, "handler", None)
    if handler is not None:
        return handler(args)
    print(f"evals.runner {args.command}: not implemented", file=sys.stderr)
    return NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
