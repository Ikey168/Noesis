#!/usr/bin/env python3
"""
Fetch the pinned pretrained backends (``make models``, #959).

Downloads every model in :mod:`src.argument_mining.model_registry` into the
local Hugging Face cache (idempotent, resumable), writes
``models/pins.lock.json`` with the resolved immutable revisions, then
reports the active prediction mode per wrapper — with the backend env
toggles set, a fresh clone goes from heuristic to model-grade analytics in
this one command:

    make models
    NOESIS_STANCE_BACKEND=nli NOESIS_FRAMES_BACKEND=nli \\
    NOESIS_CLAIMS_BACKEND=pretrained python3 ...

No weights land in git; the heuristic fallback keeps working fully offline.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    from src.argument_mining.model_registry import backend_status, fetch_models

    summary = fetch_models()
    for entry in summary["fetched"]:
        print(f"[ok]   {entry['backend']}: {entry['model']} @ {entry['revision']}")
    for entry in summary["failed"]:
        print(f"[fail] {entry['backend']}: {entry['model']} — {entry['error']}",
              file=sys.stderr)
    for warning in summary["warnings"]:
        print(f"[warn] {warning}", file=sys.stderr)
    print(f"[lock] {summary['lock_path']}")

    # Activate the pretrained tiers for the status report so it shows what
    # a configured install will actually run.
    os.environ.setdefault("NOESIS_STANCE_BACKEND", "nli")
    os.environ.setdefault("NOESIS_FRAMES_BACKEND", "nli")
    os.environ.setdefault("NOESIS_CLAIMS_BACKEND", "pretrained")
    print("\nActive backends:")
    for name, mode in backend_status().items():
        print(f"  {name:<7} {mode}")

    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
