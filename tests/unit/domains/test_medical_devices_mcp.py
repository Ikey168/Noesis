"""Medical-devices MCP entry points: catalog registration, declared scopes, exclusions and minimised answers
(#2714, MD12)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.medical_devices_records import NARRATIVE_SCOPE, forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import medical_devices_fixture_builder as fb
from tests.unit import medical_devices_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import CLINICAL_TOOLS
from tools.knowledge_engine_mcp.medical_devices import (
    MEDICAL_DEVICES_SCOPES,
    MEDICAL_DEVICES_TOOLS,
    MEDICAL_DEVICES_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "medical-devices-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_ownership(conn)
    h.load_product_safety(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert MEDICAL_DEVICES_TOOLS <= set(tools) and MEDICAL_DEVICES_TOOLS <= CLINICAL_TOOLS
    assert set(MEDICAL_DEVICES_SCOPES) == MEDICAL_DEVICES_TOOLS
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in MEDICAL_DEVICES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in MEDICAL_DEVICES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == MEDICAL_DEVICES_SCOPES[name]
        assert by_name[name]["required_scopes"] == MEDICAL_DEVICES_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/clinical-evidence/providers/clinical.devices.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == MEDICAL_DEVICES_TOOLS
    # tools declare the exclusions
    for name in ("medical_device_regulatory_history", "medical_device_adverse_event_counts"):
        description = " ".join(tools[name].description.lower().split())
        assert "no safety-signal detection" in description and "clinical advice" in description
    assert "never rates" in " ".join(tools["medical_device_adverse_event_counts"].description.split())


def test_history_counts_identity_links_bundles_and_monitors_through_mcp(mcp_env):
    tools, state = mcp_env
    history = tools["medical_device_regulatory_history"].fn(namespace=h.NS, subject="K999901", as_of="2099-12-31")
    assert history["status"] == "answered" and history["boundary"] and forbidden_keys(history) == []
    counts = tools["medical_device_adverse_event_counts"].fn(namespace=h.NS, subject="ZZA")
    assert counts["unit"] == "reports" and counts["caveats"] and forbidden_keys(counts) == []
    record = tools["medical_device_record_history"].fn(namespace=h.NS, record_key="medical-devices:fda:maude:"
                                                                                  "9999901-2099-00001")
    assert record["revisions"][0]["record"]["fields"]["narratives"] is None  # withheld without the scope
    state["scopes"] = set(h.REVIEW_SCOPES) | {NARRATIVE_SCOPE}
    record = tools["medical_device_record_history"].fn(namespace=h.NS, record_key="medical-devices:fda:maude:"
                                                                                  "9999901-2099-00001")
    assert record["revisions"][0]["record"]["fields"]["narratives"][0]["as_published"] is True
    proposed = tools["propose_medical_device_identity_matches"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    (udi,) = [c for c in proposed["candidates"] if c["method"] == "udi-di"]
    state["principal"] = "bob"
    reviewed = tools["review_medical_device_identity_match"].fn(namespace=h.NS, candidate_id=udi["candidate_id"],
                                                                decision="accept", reason="same GS1 DI")
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    linked = tools["link_medical_device_records"].fn(namespace=h.NS, kinds=["product-safety"],
                                                     products_namespace=h.PRODUCTS_NS)
    assert linked["results"][0]["status"] == "linked"
    assert tools["list_medical_device_links"].fn(namespace=h.NS, kind="product-safety")["links"]
    bundle = tools["export_medical_devices_evidence_bundle"].fn(namespace=h.NS, query="history",
                                                               subject=h.FIXTURE_DI, as_of="2099-12-31")
    assert bundle["evidence_bundle"]["bibliography"] and bundle["answer"]["jurisdictions"]["EU"]["devices"]
    monitor = tools["create_medical_devices_monitor"].fn(namespace=h.NS, request_key="mcp", watch="product-code",
                                                         key="ZZA")
    assert monitor["subscription_id"]
    unmatched = tools["list_medical_devices_unmatched"].fn(namespace=h.NS)
    assert unmatched["unmatched"]
    contracts = tools["medical_devices_source_contracts"].fn()
    assert contracts["live_verification"]["eudamed"]["status"] == "unverified-live"
    assert contracts["minimisation"]["policy"] == "medical-devices-minimisation-v1"
    everything = json.dumps([history, counts, proposed, linked, bundle, unmatched])
    assert not [p for p in fb.PERSONAL if p in everything]
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_medical_device_identity_match"].fn(namespace=h.NS, candidate_id=udi["candidate_id"],
                                                               reason="x")
    assert refused["ok"] is False
