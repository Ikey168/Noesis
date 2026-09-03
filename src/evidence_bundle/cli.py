"""Command-line interface for portable evidence bundles."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .verifier import INCOMPLETE, verify_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noesis")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify an evidence bundle offline")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--schema", type=Path)
    verify.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "verify":  # pragma: no cover - argparse enforces this
        return 1
    kwargs = {"schema_path": args.schema} if args.schema else {}
    result = verify_file(args.bundle, **kwargs)
    if args.as_json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(f"{result.status}: {args.bundle}")
        for message in result.errors:
            print(f"ERROR: {message}")
        for message in result.warnings:
            print(f"WARNING: {message}")
    if result.valid:
        return 0
    return 2 if result.status == INCOMPLETE else 1
