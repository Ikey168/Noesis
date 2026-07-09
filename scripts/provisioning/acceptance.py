"""
R9 provisioned-domain acceptance harness (Track P acceptance, issues #609/#610).

Stands up two knowledge domains - **finance** (earnings-call transcripts) and
**legal** (policy filings) - over the real ``provisioning_mcp`` server, using
only the provisioning verbs and writing no pack code. It seeds a throwaway
warehouse, drives ``kg_deploy`` / ``kg_attach_sources`` / ``kg_ingest`` /
``kg_status`` / ``kg_view`` / ``kg_lineage`` through a FastMCP client, and
asserts each domain is live, scoped, and discoverable. Prints a step trace with
per-domain time-to-live.

Run:  python scripts/provisioning/acceptance.py

This is the executable form of docs/milestones/provisioning.md.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)
os.environ.pop("TESTING", None)


def _seed(db: str) -> None:
    import duckdb

    con = duckdb.connect(db)
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    con.executemany(
        "INSERT INTO news_articles (id, title, url, content, publish_date, source, category) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("f1", "Acme Corp Q3 earnings call transcript", "u1", "c", "2026-06-14", "Acme Earnings", "earnings"),
            ("f2", "Acme Corp raises full-year guidance", "u2", "c", "2026-06-14", "Acme Earnings", "earnings"),
            ("f3", "Globex investor call on margins", "u3", "c", "2026-06-13", "Globex Calls", "earnings"),
            ("l1", "Proposed rule on emissions disclosure", "u4", "c", "2026-06-12", "Federal Register", "policy"),
            ("l2", "Comment period opens for privacy rule", "u5", "c", "2026-06-11", "Federal Register", "policy"),
            ("n1", "Local election results tonight", "u6", "c", "2026-06-10", "City News", "politics"),
        ],
    )
    con.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, "
        "document_id VARCHAR, source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    con.executemany(
        "INSERT INTO argument_claims VALUES (?,?,?,?,?,?)",
        [
            ("cf1", "Cloud revenue grew 34 percent.", "f1", "transcript", 0.9, "supported"),
            ("cf2", "Guidance assumes no rate hikes.", "f2", "transcript", 0.7, "unverified"),
            ("cl1", "The rule takes effect in 90 days.", "l1", "legal", 0.85, "supported"),
            ("cn1", "Turnout was a record high.", "n1", "news", 0.6, None),
        ],
    )
    con.execute(
        "CREATE TABLE outlet_scores (source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, stance_neutrality DOUBLE, "
        "composite_score DOUBLE, doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    con.executemany(
        "INSERT INTO outlet_scores VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("Acme Earnings", "news", "2026-06-14", 0.8, 0.9, 0.7, 0.81, 30, 20, "2026-06-14"),
            ("Globex Calls", "news", "2026-06-14", 0.7, 0.8, 0.7, 0.76, 20, 12, "2026-06-14"),
            ("Federal Register", "news", "2026-06-14", 0.9, 0.95, 0.8, 0.9, 40, 25, "2026-06-14"),
        ],
    )
    con.close()


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


async def _provision(client, name, description, *, sources=None, criteria=None):
    t0 = time.monotonic()
    # Deploy is approval-gated: the preview writes nothing.
    preview = (await client.call_tool("kg_deploy", {"name": name, "description": description})).structured_content
    assert preview.get("preview") is True, preview
    dep = (await client.call_tool("kg_deploy", {"name": name, "description": description, "approve": True})).structured_content
    assert dep["deployed"], dep
    args = {"kg": name}
    if criteria is not None:
        args["criteria"] = criteria
    if sources is not None:
        args["sources"] = sources
    at = (await client.call_tool("kg_attach_sources", args)).structured_content
    assert "error" not in at, at
    ing = (await client.call_tool("kg_ingest", {"kg": name})).structured_content
    assert ing["ingested"], ing
    ttl = time.monotonic() - t0
    print(
        f"  [{name}] deploy -> attach ({len(at['sources'])} sources) -> ingest "
        f"({ing['routed']['documents']} docs, {ing['totals']['claims']} claims, "
        f"{ing['totals']['entities']} entities) in {ttl*1000:.0f} ms"
    )
    return ing, ttl


async def _run(db: str) -> None:
    prov = _load("prov_srv", REPO_ROOT / "tools/provisioning_mcp/server.py")
    from fastmcp.client import Client

    async with Client(prov.mcp) as c:
        print("Standing up two domains by provisioning alone (no pack code):")
        await _provision(c, "finance", "Earnings-call transcripts",
                         sources=["Acme Earnings", "Globex Calls"])
        await _provision(c, "legal", "Policy and rule filings",
                         criteria={"min_transparency": 0.85, "type": "news"})

        # Both live and scoped.
        listing = (await c.call_tool("kg_list", {})).structured_content
        names = sorted(k["name"] for k in listing["kgs"])
        assert names == ["finance", "legal"], names
        print(f"kg_list: {listing['count']} domains live -> {names}")

        for name, docs in (("finance", 3), ("legal", 2)):
            view = (await c.call_tool("kg_view", {"kg": name})).structured_content
            kg = view["kgs"][0]
            fam = kg["sample"]
            assert kg["counts"]["documents"] == docs, kg["counts"]
            assert fam["documents"] and fam["entities"], fam
            events = {e["event"] for e in (await c.call_tool("kg_lineage", {"kg": name})).structured_content["events"]}
            assert {"deploy", "attach", "ingest"} <= events, events
            print(
                f"  [{name}] scoped family: {len(fam['documents'])} docs, "
                f"{len(fam['entities'])} entities, {len(fam['claims'])} claims; "
                f"lineage {sorted(events)}"
            )

        # Shared corpus untouched.
        import duckdb

        ro = duckdb.connect(db, read_only=True)
        shared = ro.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        ro.close()
        assert shared == 6, shared
        print(f"shared news_articles still {shared} rows (untouched)")


def main() -> None:
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "wh.duckdb")
    os.environ["NEURONEWS_DB_PATH"] = db
    _seed(db)
    asyncio.run(_run(db))
    print("RESULT: OK - two domains live via provisioning alone (R9 exit criterion)")


if __name__ == "__main__":
    main()
