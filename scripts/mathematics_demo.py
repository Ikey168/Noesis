#!/usr/bin/env python3
"""Reproducible offline mathematics research demo (no network).

Installs the research-discovery source pack in a throwaway warehouse, replays
the pinned zbMATH Open, OEIS, mathlib4 and AFP fixtures through the real
runtime and projection, records explicit and candidate links, and answers:

    "What is known about Zeckendorf representations, in the literature and in
     formal libraries?"

The answer lists papers, sequences and formal declarations with their source
records, pinned library commits and link evidence. Its ``answer_sha256`` is
stable across runs; use ``--output`` to write the full answer.

    python scripts/mathematics_demo.py --output /tmp/zeckendorf-answer.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

QUESTION = "Zeckendorf representation"
MATH_SOURCES = {"zbmath-open": "documents", "oeis-sequences": "sequences", "mathlib4-fib": "snapshot",
                "afp-zeckendorf": "snapshot"}
SCOPES = {"knowledge:mathematics:read", "knowledge:mathematics:write"}


def answer() -> dict:
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.mathematics import MathStore

    manifest = validate_source_pack(json.loads((REPO_ROOT / "config/source_packs/research.json").read_text()))
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(manifest, principal_id="demo", enable=True, now_ms=1)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    receipts = {}
    for source_id, operation in MATH_SOURCES.items():
        runtime.accept_license(manifest["pack_id"], source_id, principal_id="demo")
        receipt = runtime.run(
            {"pack_id": manifest["pack_id"], "run_key": f"demo:{source_id}", "operation": operation,
             "source_ids": [source_id], "max_pages": 50, "max_results": 50, "max_bytes": 5_000_000,
             "timeout_ms": 30_000, "mode": "backfill", "backfill": {"from_ms": 0}},
            principal_id="demo", adapters=runtime.fixture_adapters(manifest["pack_id"], REPO_ROOT),
            dns_resolver=lambda _h: ["8.8.8.8"])
        receipts[source_id] = receipt["sources"][0]["status"]
    store = MathStore(conn, now=lambda: 1)
    store.propose_links(scopes=SCOPES, principal_id="demo")
    found = store.search(QUESTION, scopes=SCOPES, limit=4)
    conn.close()

    def brief(item: dict) -> dict:
        record = item["record"]
        head = {"kind": item["kind"], "id": item["id"], "revision": item["revision"], "access": item["access"]}
        if item["kind"] == "literature":
            head.update(title=record["title"], identifiers=record["identifiers"])
        elif item["kind"] == "sequence":
            head.update(name=record["name"], first_terms=record["terms"][:10])
        else:
            head.update(name=record["name"], statement=record["statement"][:200], permalink=record["permalink"])
        head["links"] = [{"basis": link["basis"], "state": link["state"], "other": link["object"]
                          if link["subject"]["id"] == item["id"] else link["subject"],
                          "confidence": link["confidence"]} for link in item["links"]][:6]
        return head

    body = {"question": QUESTION, "source_runs": receipts, "total_matches": found["total"],
            "results": [brief(item) for item in found["results"]], "notice": found["notice"]}
    return {**body, "answer_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                                                    .encode()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = answer()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    for item in result["results"]:
        label = item.get("title") or item.get("name")
        print(f"[{item['kind']}] {item['id'][:70]} — {label[:80]}")
        for link in item["links"][:3]:
            print(f"    {link['state']:9} {link['basis']:20} -> {link['other']['id'][:70]}")
    print(f"answer_sha256 {result['answer_sha256']}")
    return 0 if all(status == "complete" for status in result["source_runs"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
