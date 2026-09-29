#!/usr/bin/env python3
"""
CI gate: fail if outbound HTTP calls appear in background/scheduler code.

Scans every Python file OUTSIDE the whitelisted directories for calls that
could silently phone home:
  - requests.get / requests.post
  - httpx.get / httpx.post / httpx.request
  - urllib.request.urlopen / urllib.request.urlretrieve

Whitelisted directories (fetching is expected behaviour there):
  - src/ingestion/
  - src/nlp/
  - src/scraper/
  - legacy/src/scraper.py

Exit 0 → clean. Exit 1 → potential telemetry calls found.

Run:
    python3 scripts/check_telemetry.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories where HTTP fetches are intentional (connectors, NLP model pulls,
# user-configured alert delivery, user-triggered fact-checks, localhost clients).
WHITELIST_PREFIXES: list[str] = [
    str(REPO_ROOT / "src" / "ingestion"),
    str(REPO_ROOT / "src" / "nlp"),
    str(REPO_ROOT / "src" / "scraper"),
    str(REPO_ROOT / "src" / "scraper.py"),
    # Alert channels: user-configured webhooks (Slack, email) — not background telemetry.
    str(REPO_ROOT / "src" / "alerts"),
    # Fact-check: user-triggered queries against external fact-check APIs.
    str(REPO_ROOT / "src" / "argument_mining"),
    # Dashboard API client: calls the local FastAPI server (localhost).
    str(REPO_ROOT / "src" / "dashboards"),
    # Demo / integration scripts: call local services (Marquez at localhost:5000).
    str(REPO_ROOT / "scripts" / "demo_"),
    # Test helpers are fine.
    str(REPO_ROOT / "tests"),
]

# Directories to skip entirely.
SKIP_PREFIXES: list[str] = [
    str(REPO_ROOT / "archive"),
    str(REPO_ROOT / ".claude"),
]

SCAN_DIRS: list[Path] = [
    REPO_ROOT / "src",
    REPO_ROOT / "scripts",
    REPO_ROOT / "services",
    REPO_ROOT / "apps",
]

# Patterns that indicate an outbound HTTP call.
_HTTP_CALLS = re.compile(
    r"""
    \b(?:
        requests\.(?:get|post|put|delete|patch|head|request)\s*\(  # requests.*()
      | httpx\.(?:get|post|put|delete|patch|head|request)\s*\(     # httpx.*()
      | urllib\.request\.(?:urlopen|urlretrieve)\s*\(              # urllib.request.*()
      | aiohttp\.ClientSession\s*\(                                 # aiohttp session
    )
    """,
    re.VERBOSE,
)

# False-positive suppression: lines that are obviously safe (comments,
# docstrings, type annotations, string literals that describe but don't call).
_SAFE_LINE = re.compile(
    r"""
    ^\s*(?:\#|\"\"\"|\'\'\')  # comment or docstring start
    | ^\s*[a-zA-Z_][\w.]*\s*[:=]  # type annotation / assignment label
    """,
    re.VERBOSE,
)


def is_whitelisted(path: Path) -> bool:
    s = str(path)
    return any(s.startswith(p) for p in WHITELIST_PREFIXES)


def is_skipped(path: Path) -> bool:
    s = str(path)
    if "__pycache__" in path.parts:
        return True
    if path == Path(__file__):  # skip this script itself
        return True
    return any(s.startswith(p) for p in SKIP_PREFIXES)


def scan() -> list[tuple[Path, int, str]]:
    violations: list[tuple[Path, int, str]] = []
    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            if is_skipped(py_file) or is_whitelisted(py_file):
                continue
            try:
                for lineno, line in enumerate(py_file.read_text(errors="replace").splitlines(), 1):
                    if _HTTP_CALLS.search(line) and not _SAFE_LINE.match(line):
                        violations.append((py_file, lineno, line.rstrip()))
            except OSError:
                pass
    return violations


def main() -> int:
    violations = scan()
    if not violations:
        print("check_telemetry: OK — no suspicious outbound HTTP calls found.")
        return 0

    print(f"check_telemetry: FAIL — {len(violations)} potential telemetry call(s):\n")
    for path, lineno, line in violations:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}  {line.strip()}")
    print(
        "\nIf these are intentional (e.g. a new connector), add the directory to "
        "WHITELIST_PREFIXES in scripts/check_telemetry.py."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
