#!/usr/bin/env python3
"""Compatibility entry point for evidence-bundle commands.

The future unified ``noesis`` CLI can delegate its ``verify`` subcommand to
``src.evidence_bundle.cli`` without changing verifier behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.evidence_bundle.cli import main  # noqa: I001


if __name__ == "__main__":
    raise SystemExit(main())
