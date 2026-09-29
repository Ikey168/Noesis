"""Bounded live acceptance run for the Berlin geospatial source pack (#1705).

Acquires both pinned WFS layers from gdi.berlin.de through the real runtime,
projects them, evaluates schools-inside-district for every district, compares
the result with each school's provider-declared district, and writes a dated,
secret-free receipt.  Raw payloads are not stored; only hashes and counts.

Usage: python scripts/geospatial_live_berlin.py [--out PATH] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.source_pack_runtime import SourcePackRuntime  # noqa: E402
from src.ingestion.source_packs import SourcePackStore, validate_source_pack  # noqa: E402
from src.kb.geospatial_features import GeospatialFeatureStore, pack_readiness  # noqa: E402

SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write",
          "knowledge:geospatial:calculate"}
DISTRICTS, SCHOOLS = "alkis_bezirke:bezirksgrenzen", "schulen:schulen"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "config/geospatial/acceptance/live-berlin.json"))
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args()
    conn = duckdb.connect(args.db)
    manifest = validate_source_pack(json.loads((ROOT / "config/source_packs/geospatial.json").read_text()))
    SourcePackStore(conn).install(manifest, principal_id=args.operator, enable=True)
    runtime = SourcePackRuntime(conn)
    terms = [
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id=args.operator)
        for source in manifest["sources"]
    ]
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    receipt = runtime.run(
        {"pack_id": manifest["pack_id"], "run_key": "live-" + started_at, "operation": "features",
         "network": "live", "max_results": 5000, "max_bytes": 5_000_000, "timeout_ms": 120_000},
        principal_id=args.operator,
    )
    elapsed = round(time.monotonic() - started, 3)
    rows = conn.execute(
        "SELECT source_id,start_index,number_matched,number_returned,provider_timestamp,response_sha256 "
        "FROM geospatial_feature_pages WHERE run_id=? ORDER BY source_id,start_index",
        [receipt["run_id"]],
    ).fetchall()
    store = GeospatialFeatureStore(conn)
    declared = Counter(
        json.loads(props)["bezirk"]
        for (props,) in conn.execute(
            "SELECT r.properties_json FROM geospatial_features f JOIN geospatial_feature_current c USING(feature_id) "
            "JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.collection=?",
            [SCHOOLS]).fetchall()
    )
    districts = []
    for name in sorted(declared):
        query_started = time.monotonic()
        result = store.within("global", collection=SCHOOLS, boundary_name=name,
                              boundary_collection=DISTRICTS, principal_id=args.operator,
                              scopes=SCOPES, limit=5000)
        members = result["members"]
        mismatched = sorted(
            m["native_id"] for m in members
            if json.loads(conn.execute("SELECT properties_json FROM geospatial_feature_revisions WHERE revision_id=?",
                                       [m["revision_id"]]).fetchone()[0])["bezirk"] != name
        )
        replay = store.replay_within("global", result["receipt"]["receipt_id"], scopes=SCOPES) if result["receipt"] else None
        districts.append({
            "district": name, "status": result["status"], "members": len(members),
            "provider_declared": declared[name], "mismatched": mismatched,
            "receipt_id": None if not result["receipt"] else result["receipt"]["receipt_id"],
            "replay_deterministic": None if replay is None else replay["deterministic"],
            "latency_ms": round((time.monotonic() - query_started) * 1000, 1),
        })
    readiness = pack_readiness(conn, manifest["pack_id"])
    agreement = all(d["members"] == d["provider_declared"] and not d["mismatched"] for d in districts)
    output = {
        "contract": "noesis-geospatial-live-acceptance-v1",
        "evidence_kind": "live-provider",
        "run_started_at": started_at,
        "pack_id": manifest["pack_id"], "pack_version": manifest["version"],
        "manifest_hash": manifest["manifest_hash"],
        "terms": [{"source_id": t["source_id"], "license_id": t["license_id"],
                   "terms_hash": t["terms_hash"], "accepted_by": t["accepted_by"]} for t in terms],
        "run": {"run_id": receipt["run_id"], "status": receipt["status"], "elapsed_s": elapsed,
                "failures": receipt["failures"]},
        "sources": [
            {"source_id": s["source_id"], "endpoint": s["adapter"]["endpoint"],
             "type_names": s["adapter"]["wfs"]["type_names"], "status": s["status"],
             "counts": s["counts"], "retries": s["retries"],
             "snapshot": {k: s["projection"][k] for k in ("completeness", "reasons", "number_matched", "states", "removed")}}
            for s in receipt["sources"]
        ],
        "pages": [{"source_id": r[0], "start_index": r[1], "number_matched": r[2], "number_returned": r[3],
                   "provider_timestamp": r[4], "response_sha256": r[5]} for r in rows],
        "points_within_by_district": districts,
        "provider_declared_agreement": agreement,
        "transform": readiness["transform"],
        "outstanding": [b for b in readiness["blockers"] if b["severity"] != "degraded"],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": receipt["status"], "elapsed_s": elapsed, "agreement": agreement,
                      "schools": sum(d["members"] for d in districts)}))
    return 0 if receipt["status"] == "complete" and agreement else 1


if __name__ == "__main__":
    raise SystemExit(main())
