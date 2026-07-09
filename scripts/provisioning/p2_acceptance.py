"""
Track P2 acceptance: two domains, each with its own database and its own live
pipeline, stood up by provisioning alone (issues #640-#644).

This is the phase-2 counterpart to `acceptance.py` (R9). It provisions two
domains through the provisioning plane, each deployed into its own attached
DuckDB database (not a table prefix in the shared warehouse) and fed by its own
bound pipeline. The pipeline runner here simulates a connector: on ingest it
writes fresh rows into the shared corpus (as a real connector would), and
routing then copies the matching rows into the KG's own database, proving the
connector to route path end to end. Each domain is reconstructed from its audit
trail at the end.

Run:  python scripts/provisioning/p2_acceptance.py

The executable form of docs/milestones/provisioning-p2.md.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def main() -> None:
    import duckdb

    from src.provisioning import namespaces, store
    from src.provisioning.provisioner import Provisioner

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "wh.duckdb")
    os.environ["NOESIS_DB_PATH"] = db
    conn = duckdb.connect(db)
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    print("seeded empty shared warehouse (connectors will fill it on ingest)")

    # A connector simulator: each run appends fresh articles for its source,
    # exactly as a real connector -> contract -> enrich step would land them in
    # the shared corpus. Routing then copies them into the KG's own database.
    def make_runner(source: str, ids):
        def runner(spec):
            n = 0
            for i in ids:
                exists = conn.execute("SELECT 1 FROM news_articles WHERE id = ?", [i]).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO news_articles (id, title, url, publish_date, source, category) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [i, f"{source} item {i}", f"http://x/{i}", _now(), source, "topic"],
                    )
                    n += 1
            return {"connector": spec["connector"], "documents_ingested": n}
        return runner

    def stand_up(name, source, ids):
        prov = Provisioner(
            conn, clock=_now, pipeline_runner=make_runner(source, ids)
        )
        dep = prov.deploy(name, f"{name} domain", approve=True, backend="attached")
        assert dep["backend"] == "attached", dep
        at = prov.attach_pipeline(
            name, f"{name}-feed", "rss", {"url": f"http://{name}/feed.xml"}, approve=True
        )
        assert at["attached"], at
        prov.attach_sources(name, sources=[source])
        ing = prov.ingest(name)
        assert ing["ingested"] and ing["backend"] == "attached", ing
        print(
            f"  [{name}] deploy(attached) -> attach_pipeline -> attach_source -> ingest: "
            f"pipeline ran ({ing['pipeline_runs'][0]['result']['documents_ingested']} ingested), "
            f"routed {ing['routed']['documents']} docs into its own database"
        )
        return prov

    print("Standing up two domains, each with its own database and pipeline:")
    prov = stand_up("energy", "Energy Feed", ["e1", "e2", "e3"])
    stand_up("markets", "Markets Feed", ["m1", "m2"])

    # Each KG's rows live in its OWN attached database, isolated from the other.
    dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    assert {"kg_energy", "kg_markets"} <= dbs, dbs
    e = conn.execute("SELECT COUNT(*) FROM kg_energy.documents").fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM kg_markets.documents").fetchone()[0]
    print(f"isolation: kg_energy.documents={e}, kg_markets.documents={m}, separate databases {sorted(d for d in dbs if d.startswith('kg_'))}")
    assert e == 3 and m == 2

    # Reconstruct each from its audit trail.
    for name in ("energy", "markets"):
        events = [ev["event"] for ev in reversed(store.list_events(conn, name, limit=50))]
        print(f"  [{name}] audit trail: {events}")
        assert events[:4] == ["deploy", "attach_pipeline", "attach", "ingest"], events

    # Teardown detaches the database and keeps the file.
    energy_file = namespaces.attached_db_path("energy")
    prov.teardown("energy", confirm=True)
    dbs2 = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    assert "kg_energy" not in dbs2 and os.path.exists(energy_file)
    print(f"teardown energy: database detached, file kept at {os.path.basename(energy_file)}")

    conn.close()
    print("RESULT: OK - two domains live via provisioning alone, each with its own database and pipeline")


if __name__ == "__main__":
    main()
