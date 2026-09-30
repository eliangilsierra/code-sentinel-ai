"""Command-line entry point for review-ctx."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from review_ctx import __version__
from review_ctx.ledger import Ledger, LedgerError
from review_ctx.repo import RepoError, repo_root, run_dir
from review_ctx.show import render_finding

EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_ABANDONED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-ctx",
        description="Deterministic context, evidence ledger and gate for review-squad.",
    )
    parser.add_argument("--version", action="version", version=f"review-ctx {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def with_run(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run", required=True, help="run id")
        command.add_argument("--repo", type=Path, default=None, help="repository path")
        return command

    with_run("emit", "record a finding read as JSON from standard input")
    show = with_run("show", "print a finding with its citations and code")
    show.add_argument("id")
    verdict = with_run("verdict", "record a verifier verdict for a finding")
    verdict.add_argument("id")
    verdict.add_argument("verdict", choices=("confirmed", "refuted", "unverifiable"))
    verdict.add_argument("--sev", default=None, help="adjusted severity")
    verdict.add_argument("--note", default="", help="reason, at most 120 characters")
    return parser


def _open_ledger(args: argparse.Namespace) -> Ledger:
    root = repo_root(args.repo)
    return Ledger(run_dir(root, args.run, create=args.command == "emit"), root)


def _emit(args: argparse.Namespace) -> int:
    result = _open_ledger(args).emit(sys.stdin.read())
    if result.ok:
        payload = {
            "id": result.id,
            "status": result.status,
            "line_corrected": result.line_corrected,
        }
        print(json.dumps(payload))
        return 0
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    return EXIT_ABANDONED if result.status == "abandoned" else EXIT_REJECTED


def _show(args: argparse.Namespace) -> int:
    print(render_finding(_open_ledger(args), args.id))
    return 0


def _verdict(args: argparse.Namespace) -> int:
    entry = _open_ledger(args).add_verdict(args.id, args.verdict, args.note, args.sev)
    print(json.dumps(entry))
    return 0


HANDLERS = {"emit": _emit, "show": _show, "verdict": _verdict}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return HANDLERS[args.command](args)
    except (RepoError, LedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
