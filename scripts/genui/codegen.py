#!/usr/bin/env python3
"""
Regenerate the genui catalog mirrors from src/genui/catalog.py.

Thin entry point; the actual generation lives in src/genui/codegen.py so it
is unit-tested and coverage-gated. Usage:

    python scripts/genui/codegen.py           # rewrite stale files
    python scripts/genui/codegen.py --check   # CI: exit 1 when stale
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.genui.codegen import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
