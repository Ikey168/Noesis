#!/usr/bin/env python3
"""
Fetch the pinned pretrained backends (``make models``, #959).

Downloads every model in :mod:`src.argument_mining.model_registry` into the
local Hugging Face cache (idempotent, resumable), writes
``models/pins.lock.json`` with the resolved immutable revisions, then
reports the active prediction mode per wrapper. A fresh clone goes from
heuristic to model-grade analytics in this one command:

    make models
    python3 ...

No weights land in git; the heuristic fallback keeps working fully offline.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify registry/lock consistency without downloading")
    parser.add_argument("--require-cache", action="store_true", help="also require both local snapshots")
    args = parser.parse_args()

    from src.argument_mining.model_registry import backend_status, fetch_models, verify_pins

    if args.check:
        warnings = verify_pins(require_cache=args.require_cache)
        for warning in warnings:
            print(f"[fail] {warning}", file=sys.stderr)
        if not warnings:
            print("[ok] model registry and lock file agree")
        return 1 if warnings else 0

    summary = fetch_models()
    for entry in summary["fetched"]:
        print(f"[ok]   {entry['backend']}: {entry['model']} @ {entry['revision']}")
    for entry in summary["failed"]:
        print(f"[fail] {entry['backend']}: {entry['model']} — {entry['error']}",
              file=sys.stderr)
    for warning in summary["warnings"]:
        print(f"[warn] {warning}", file=sys.stderr)
    print(f"[lock] {summary['lock_path']}")

    print("\nActive backends:")
    for name, mode in backend_status().items():
        print(f"  {name:<7} {mode}")

    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
