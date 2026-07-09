"""
M3 acceptance: stand up a domain KG end to end from a live connector run.

Unlike the P2 acceptance (which used an inline simulator for the runner), this
uses the real M3.1 pipeline runner (`build_pipeline_runner`): on ingest it runs
the bound connector for real, persists the harvested documents into the shared
corpus, and routing copies them into the KG namespace. There is no simulation
branch on this path.

The harvester defaults to a small bundled feed sample so the run is reproducible
in CI. Set ``NOESIS_M3_LIVE=1`` (with a network-reachable connector, e.g. the
RSS ``news`` connector) to harvest from a live feed instead; either way the
runner's persist-and-route is the real path.

Run:  python scripts/provisioning/m3_acceptance.py

The executable form of docs/milestones/provisioning-m3.md.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)


def _now():
    return datetime(2026, 7, 3, tzinfo=timezone.utc)


# A small bundled feed sample (stands in for a live climate feed when
# NOESIS_M3_LIVE is not set). Each item is a real document the connector path
# persists and routes; nothing is faked downstream of the harvest.
_SAMPLE = [
    ("clim-1", "Grid operators report record renewable share", "2026-06-28"),
    ("clim-2", "New storage tariff clears regulatory review", "2026-06-29"),
    ("clim-3", "Offshore wind auction sets a price floor", "2026-06-30"),
    ("clim-4", "Heat wave stresses the interconnect", "2026-07-01"),
]


def _sample_harvester(connector_type, config):
    source = (config or {}).get("source") or "Climate Wire"
    return [
        {"id": i, "title": t, "url": f"https://climate.example/{i}",
         "content": t, "publish_date": d, "source": source}
        for (i, t, d) in _SAMPLE
    ]


def main() -> dict:
    import duckdb

    from src.provisioning import namespaces, store
    from src.provisioning.pipeline_runner import build_pipeline_runner, default_harvester
    from src.provisioning.provisioner import Provisioner
    from src.osint import investigation_audit

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "wh.duckdb")
    os.environ["NEURONEWS_DB_PATH"] = db
    conn = duckdb.connect(db)
    store.ensure_schema(conn)

    live = os.getenv("NOESIS_M3_LIVE") == "1"
    harvester = default_harvester if live else _sample_harvester
    runner = build_pipeline_runner(conn, harvester=harvester)
    prov = Provisioner(conn, clock=_now, pipeline_runner=runner)

    print("M3 acceptance: standing up a domain from a "
          + ("live feed" if live else "bundled feed sample") + "\n")

    dep = prov.deploy("climate", "Climate and energy coverage", approve=True)
    assert dep.get("deployed"), dep
    connector_type = "news" if live else "rss"
    at = prov.attach_pipeline(
        "climate", connector="climate-feed", connector_type=connector_type,
        config={"url": "https://climate.example/rss", "source": "Climate Wire"},
        approve=True,
    )
    assert at.get("attached"), at
    prov.attach_sources("climate", sources=["Climate Wire"])
    ing = prov.ingest("climate")
    assert ing.get("ingested"), ing

    run = ing["pipeline_runs"][0]
    print(f"connector run: {run['connector']} ok={run['ok']} "
          f"fetched={run['result'].get('fetched')} written={run['result'].get('written')}")
    routed = ing["totals"]["documents"]
    print(f"routed into kg_climate namespace: {routed} documents")

    # The connector really wrote into the shared corpus, and routing copied
    # the matching rows into the KG's own namespace tables.
    corpus = conn.execute(
        "SELECT COUNT(*) FROM news_articles WHERE source = 'Climate Wire'"
    ).fetchone()[0]
    view = namespaces.namespace_sample(conn, "climate")
    print(f"corpus rows: {corpus}; kg_view documents sample: {len(view['documents'])}")

    # Fully reconstructable from the audit trail, including the connector run.
    audit = investigation_audit(conn, "climate")
    events = [e["event"] for e in audit["audit_trail"]]
    print(f"audit trail: {events}")

    ok = (
        run["ok"]
        and routed > 0
        and corpus > 0
        and "pipeline_run" in events
        and audit["reconstructable"]
    )
    result = {
        "live": live,
        "connector": run["connector"],
        "fetched": run["result"].get("fetched"),
        "written": run["result"].get("written"),
        "routed": routed,
        "corpus": corpus,
        "events": events,
        "ok": bool(ok),
    }
    print("\nRESULT: " + ("OK - domain stood up from a real connector run, no simulation"
                          if ok else "FAIL"))
    conn.close()
    return result


if __name__ == "__main__":
    out = main()
    sys.exit(0 if out["ok"] else 1)
