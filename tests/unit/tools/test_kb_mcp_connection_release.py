"""KB requests must not keep the shared warehouse write-locked."""

import subprocess
import sys

from src.database import local_analytics_connector as warehouse
from tools.kb_mcp import server


def test_kb_request_releases_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.duckdb"
    warehouse.close_shared_connection()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setattr(warehouse, "ensure_schema_and_seed", lambda conn: None)

    def read_one():
        return {"value": warehouse.get_shared_connection().execute("SELECT 1").fetchone()[0]}

    try:
        assert server._run(read_one) == {"value": 1}
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import duckdb, sys; "
                "conn = duckdb.connect(sys.argv[1], read_only=True); "
                "assert conn.execute('SELECT 1').fetchone()[0] == 1; "
                "conn.close()",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert server._run(read_one) == {"value": 1}
    finally:
        warehouse.close_shared_connection()
