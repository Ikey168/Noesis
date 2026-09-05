"""Capture actual incoming/outgoing citations and replay durable graph imports."""

import argparse
import json
import time
from pathlib import Path

import duckdb

from src.ingestion.opencitations import CitationAcquisitionStore, traverse_citations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--identifier", default="doi:10.1186/1756-8722-6-59")
    args = parser.parse_args()
    out = Path(args.out)
    captures = out.with_suffix(".captures")
    captures.mkdir(parents=True, exist_ok=True)
    cases = []
    for direction in ("references", "citations"):
        conn = duckdb.connect(args.database)
        store = CitationAcquisitionStore(conn)
        started = time.perf_counter()
        first = store.acquire(args.identifier, direction=direction, page_size=20)
        fetch_import_ms = (time.perf_counter() - started) * 1000
        snapshot = conn.execute(
            "SELECT snapshot_json FROM citation_provider_snapshots WHERE snapshot_sha256=?",
            [first["snapshot_sha256"]],
        ).fetchone()[0]
        (captures / (direction + ".json")).write_text(snapshot + "\n")
        conn.close()
        # Reopen to prove the checkpoint doesn't depend on an in-memory graph.
        conn = duckdb.connect(args.database)
        store = CitationAcquisitionStore(conn)
        cursor = first["next_cursor"]
        while cursor:
            cursor = store.acquire(
                args.identifier,
                direction=direction,
                snapshot_sha256=first["snapshot_sha256"],
                cursor=cursor,
            )["next_cursor"]
        replay = store.acquire(
            args.identifier,
            direction=direction,
            snapshot_sha256=first["snapshot_sha256"],
        )
        graph = traverse_citations(
            conn, args.identifier, direction=direction, limit=1000
        )
        cases.append(
            {
                "direction": direction,
                "acquisition": first,
                "fetch_first_import_ms": fetch_import_ms,
                "restart_resume_complete": True,
                "replay_imported": replay["imported"],
                "traversal_edge_count": graph["edge_count"],
                "traversal_bounded": graph["bounded"],
            }
        )
        conn.close()
    out.write_text(
        json.dumps(
            {"provider": "opencitations", "api_version": "2.2.0", "cases": cases},
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(cases))


if __name__ == "__main__":
    main()
