"""Fixture-only public source-pack upgrade tool workflow."""

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from tools.knowledge_engine_mcp import source_pack_upgrades

ROOT = Path(__file__).resolve().parents[3]


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn
        return keep


def test_public_preview_to_apply_to_receipt(monkeypatch):
    conn = duckdb.connect(":memory:")
    old = json.loads((ROOT / "config/source_packs/research.json").read_text())
    SourcePackStore(conn).install(old, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in old["sources"]:
        runtime.accept_license(old["pack_id"], source["source_id"], principal_id="operator")
    newer = copy.deepcopy(old)
    newer["version"] = "1.3.0"
    newer["description"] += " Fixture upgrade."
    tools = Tools()
    seen = []

    def safe(fn, *, write=False, required_scope=None):
        seen.append((write, required_scope))
        return fn(conn)

    source_pack_upgrades.register(tools, safe, lambda: ("operator", {"operator"}))
    monkeypatch.setattr("src.ingestion.source_pack_runtime.socket.getaddrinfo",
                        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))])
    preview = tools.functions["preview_source_pack_upgrade_impact"](newer)
    receipt = tools.functions["apply_source_pack_upgrade"](
        newer, preview["preview"]["preview_hash"], preview["impact_hash"], "mcp-apply")
    inspected = tools.functions["inspect_source_pack_upgrade_receipt"](old["pack_id"], "mcp-apply")
    assert receipt["receipt_hash"] == inspected["receipt_hash"]
    assert inspected["current_matches_candidate"]
    assert seen == [(False, "knowledge:read"), (True, None), (False, "operator")]
    conn.close()
