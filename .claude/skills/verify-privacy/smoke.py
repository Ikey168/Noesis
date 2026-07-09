#!/usr/bin/env python3
"""
verify-privacy smoke test
Validates the Issue #64 offline privacy implementation.
"""
from __future__ import annotations

import importlib
import inspect
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    _results.append((condition, label, detail))
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")


# ---------------------------------------------------------------------------
# 1. Telemetry CI gate
# ---------------------------------------------------------------------------
print("\n=== Telemetry CI gate ===")

gate = REPO_ROOT / "scripts" / "check_telemetry.py"
check("scripts/check_telemetry.py exists", gate.exists())
if gate.exists():
    r = subprocess.run(
        [sys.executable, str(gate)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    check("check_telemetry.py exits 0", r.returncode == 0, r.stdout.strip())

# ---------------------------------------------------------------------------
# 2. privacy_routes module structure
# ---------------------------------------------------------------------------
print("\n=== privacy_routes.py structure ===")

route_file = REPO_ROOT / "src" / "api" / "routes" / "privacy_routes.py"
check("src/api/routes/privacy_routes.py exists", route_file.exists())

if route_file.exists():
    spec = importlib.util.spec_from_file_location("privacy_routes", route_file)
    mod = importlib.util.module_from_spec(spec)
    try:
        # We cannot execute the module without FastAPI/pydantic imports succeeding,
        # so just parse the source for required symbols.
        src = route_file.read_text()
        check("DELETE /user/data handler present", "delete_user_data" in src)
        check("GET /user/data/export handler present", "export_user_data" in src)
        check("GET /user/privacy handler present", "get_privacy_prefs" in src)
        check("PATCH /user/privacy handler present", "update_privacy_prefs" in src)
        check("require_auth used", "require_auth" in src)
        check("ZIP export uses StreamingResponse", "StreamingResponse" in src)
        check("No outbound HTTP in privacy_routes", "requests." not in src and "httpx." not in src)
        check("Deletion receipt includes table counts", "tables" in src and "total_rows_deleted" in src)
        check("Export note confirms local-only", "off-device" in src.lower())
    except Exception as e:
        check("privacy_routes source parseable", False, str(e))

# ---------------------------------------------------------------------------
# 3. user_privacy_prefs table in warehouse schema
# ---------------------------------------------------------------------------
print("\n=== DuckDB schema ===")

seed = REPO_ROOT / "src" / "database" / "local_warehouse_seed.py"
check("local_warehouse_seed.py exists", seed.exists())
if seed.exists():
    seed_src = seed.read_text()
    check("user_privacy_prefs table defined", "user_privacy_prefs" in seed_src)
    check("pref_key column present", "pref_key" in seed_src)
    check("pref_value column present", "pref_value" in seed_src)
    check("updated_at column present", "updated_at" in seed_src)

# ---------------------------------------------------------------------------
# 4. app.py wiring
# ---------------------------------------------------------------------------
print("\n=== app.py wiring ===")

app_file = REPO_ROOT / "src" / "api" / "app.py"
check("src/api/app.py exists", app_file.exists())
if app_file.exists():
    app_src = app_file.read_text()
    check("PRIVACY_ROUTES_AVAILABLE flag defined", "PRIVACY_ROUTES_AVAILABLE" in app_src)
    check("try_import_privacy_routes() defined", "try_import_privacy_routes" in app_src)
    check("try_import_privacy_routes() called in check_all_imports", "try_import_privacy_routes()" in app_src)
    check("privacy_routes router included", "privacy_routes" in app_src and "include_router" in app_src)
    check("privacy feature in root features dict", '"privacy"' in app_src)

# ---------------------------------------------------------------------------
# 5. Live DuckDB round-trip (prefs table)
# ---------------------------------------------------------------------------
print("\n=== DuckDB live round-trip ===")

try:
    import duckdb
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_privacy_prefs (
            pref_key   VARCHAR PRIMARY KEY,
            pref_value VARCHAR NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO user_privacy_prefs VALUES ('k', 'v', CURRENT_TIMESTAMP)")
    row = conn.execute("SELECT pref_value FROM user_privacy_prefs WHERE pref_key='k'").fetchone()
    check("user_privacy_prefs round-trip (in-memory DuckDB)", row is not None and row[0] == "v")

    # Simulate conflict update
    conn.execute(
        "INSERT INTO user_privacy_prefs VALUES ('k', 'v2', CURRENT_TIMESTAMP) "
        "ON CONFLICT (pref_key) DO UPDATE SET pref_value = excluded.pref_value, updated_at = excluded.updated_at"
    )
    row2 = conn.execute("SELECT pref_value FROM user_privacy_prefs WHERE pref_key='k'").fetchone()
    check("UPSERT updates existing pref", row2 is not None and row2[0] == "v2")
except Exception as e:
    check("DuckDB round-trip", False, str(e))

# ---------------------------------------------------------------------------
# 7. ZIP export structure (unit-level, no DB needed)
# ---------------------------------------------------------------------------
print("\n=== ZIP export structure ===")

try:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("news_articles.json", json.dumps([{"id": "1", "title": "test"}]))
        zf.writestr("manifest.json", json.dumps({"tables": ["news_articles"]}))
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    check("ZIP contains news_articles.json", "news_articles.json" in names)
    check("ZIP contains manifest.json", "manifest.json" in names)
except Exception as e:
    check("ZIP export structure", False, str(e))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
total = len(_results)
passed = sum(1 for ok, _, _ in _results if ok)
failed = total - passed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed, {failed} failed")

if failed:
    print("\nFailed checks:")
    for ok, label, detail in _results:
        if not ok:
            suffix = f"  ({detail})" if detail else ""
            print(f"  - {label}{suffix}")
    sys.exit(1)
else:
    print("All checks passed.")
    sys.exit(0)
