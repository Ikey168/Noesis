"""
M4 acceptance: two tenants provisioned and ingested concurrently, isolated.

Two tenants (acme, globex) run their full provisioning lifecycle interleaved on
the same shared warehouse (the API owns a single writer, so "concurrent" is
interleaved operations, not parallel writes). The harness asserts:

  * each tenant lists and acts on only its own namespaces;
  * cross-tenant reads and writes are refused;
  * quotas are counted per tenant (one tenant's budget never blocks the other);
  * each tenant's ingest routes only its own documents.

Each tenant is reconstructed from its own audit trail at the end.

Run:  python scripts/provisioning/m4_acceptance.py

The executable form of docs/milestones/provisioning-m4.md.
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


def _harvester(connector_type, config):
    source = (config or {}).get("source") or "Feed"
    return [
        {"id": f"{source}-{i}", "title": f"{source} item {i}", "url": f"http://{i}",
         "content": "c", "publish_date": "2026-06-20", "source": source}
        for i in range(3)
    ]


def main() -> dict:
    import duckdb

    from src.provisioning import store
    from src.provisioning.pipeline_runner import build_pipeline_runner
    from src.provisioning.provisioner import Provisioner
    from src.osint import investigation_audit

    # One KG per tenant is each tenant's whole budget; proves independent quotas.
    os.environ["NOESIS_PROV_MAX_KGS"] = "1"

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "wh.duckdb")
    os.environ["NEURONEWS_DB_PATH"] = db
    conn = duckdb.connect(db)
    store.ensure_schema(conn)
    runner = build_pipeline_runner(conn, harvester=_harvester)

    acme = Provisioner(conn, clock=_now, tenant="acme", pipeline_runner=runner)
    globex = Provisioner(conn, clock=_now, tenant="globex", pipeline_runner=runner)

    print("M4 acceptance: two tenants provisioned and ingested concurrently\n")

    # Interleave the two tenants' lifecycles.
    steps = [
        ("acme", lambda: acme.deploy("acme_energy", "acme energy", approve=True)),
        ("globex", lambda: globex.deploy("globex_markets", "globex markets", approve=True)),
        ("acme", lambda: acme.attach_pipeline("acme_energy", connector="a-feed",
                                              connector_type="rss",
                                              config={"url": "http://a", "source": "Acme Feed"},
                                              approve=True)),
        ("globex", lambda: globex.attach_pipeline("globex_markets", connector="g-feed",
                                                  connector_type="rss",
                                                  config={"url": "http://g", "source": "Globex Feed"},
                                                  approve=True)),
        ("acme", lambda: acme.attach_sources("acme_energy", sources=["Acme Feed"])),
        ("globex", lambda: globex.attach_sources("globex_markets", sources=["Globex Feed"])),
        ("acme", lambda: acme.ingest("acme_energy")),
        ("globex", lambda: globex.ingest("globex_markets")),
    ]
    for who, step in steps:
        out = step()
        assert not out.get("error"), (who, out)

    # 1) Each tenant lists only its own namespace.
    acme_kgs = [k["name"] for k in acme.list_kgs()["kgs"]]
    globex_kgs = [k["name"] for k in globex.list_kgs()["kgs"]]
    print(f"acme sees {acme_kgs}; globex sees {globex_kgs}")
    assert acme_kgs == ["acme_energy"] and globex_kgs == ["globex_markets"]

    # 2) Cross-tenant reads and writes are refused.
    cross_read = acme.status("globex_markets").get("code")
    cross_write = globex.ingest("acme_energy").get("code")
    print(f"cross-tenant read refused: {cross_read}; cross-tenant write refused: {cross_write}")
    assert cross_read == "not_found"
    assert cross_write in {"not_found", "not_deployed"}

    # 3) Quotas are per tenant: each hit its own 1-KG budget, neither blocked the
    #    other; a second KG for either is now refused.
    acme_over = acme.deploy("acme_two", approve=True).get("code", "")
    globex_over = globex.deploy("globex_two", approve=True).get("code", "")
    print(f"per-tenant quota: acme second deploy {acme_over}, globex second deploy {globex_over}")
    assert acme_over.startswith("quota") and globex_over.startswith("quota")

    # 4) Each tenant's ingest routed only its own documents.
    acme_docs = acme.status("acme_energy")["counts"]["documents"]
    globex_docs = globex.status("globex_markets")["counts"]["documents"]
    print(f"routed documents: acme={acme_docs}, globex={globex_docs}")
    assert acme_docs == 3 and globex_docs == 3

    # 5) Each tenant reconstructable from its own audit trail.
    acme_trail = [e["event"] for e in investigation_audit(conn, "acme_energy")["audit_trail"]]
    globex_trail = [e["event"] for e in investigation_audit(conn, "globex_markets")["audit_trail"]]
    print(f"acme trail: {acme_trail}")
    print(f"globex trail: {globex_trail}")

    ok = (
        acme_kgs == ["acme_energy"]
        and globex_kgs == ["globex_markets"]
        and cross_read == "not_found"
        and acme_over.startswith("quota")
        and acme_docs == 3
        and globex_docs == 3
        and "pipeline_run" in acme_trail
    )
    print("\nRESULT: " + ("OK - two tenants isolated, concurrent, with independent quotas"
                          if ok else "FAIL"))
    conn.close()
    return {
        "acme_kgs": acme_kgs,
        "globex_kgs": globex_kgs,
        "cross_read": cross_read,
        "acme_docs": acme_docs,
        "globex_docs": globex_docs,
        "ok": bool(ok),
    }


if __name__ == "__main__":
    out = main()
    sys.exit(0 if out["ok"] else 1)
