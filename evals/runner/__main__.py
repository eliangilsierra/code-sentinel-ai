"""Command-line entry point: python -m evals.runner <command>."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from evals.runner.lint import collect_case_files, lint_files

COMMANDS = ("run", "judge", "report", "calibrate", "case-lint", "materialize", "budget")
NOT_IMPLEMENTED = 3
DEFAULT_CASES_DIR = Path("evals/cases")


def _case_lint(args: argparse.Namespace) -> int:
    paths = args.paths or [DEFAULT_CASES_DIR]
    missing = [path for path in paths if not path.exists()]
    if missing:
        print(f"case-lint: path not found: {missing[0]}", file=sys.stderr)
        return 2
    files = collect_case_files(paths)
    issues = lint_files(files)
    for issue in issues:
        print(issue)
    print(f"case-lint: {len(files)} case(s) checked, {len(issues)} issue(s)")
    return 1 if issues else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = subcommands.add_parser(name)
        if name == "case-lint":
            sub.add_argument("paths", nargs="*", type=Path)
            sub.set_defaults(handler=_case_lint)
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
