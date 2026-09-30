"""Platform-transparency MCP entry points: catalog registration, declared scopes, exclusions and minimised answers
(#2641)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.platform_transparency_records import (
    PlatformTransparencyError,
    forbidden_keys,
)
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import platform_transparency_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.platform_transparency import (
    PLATFORM_TRANSPARENCY_SCOPES,
    PLATFORM_TRANSPARENCY_TOOLS,
    PLATFORM_TRANSPARENCY_WRITES,
    guarded,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "platform-transparency-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_campaign_finance(conn)
    h.load_ownership(conn)
    h.load_lobbying(conn)
    h.load_elections(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert PLATFORM_TRANSPARENCY_TOOLS <= set(tools)
    assert set(PLATFORM_TRANSPARENCY_SCOPES) == PLATFORM_TRANSPARENCY_TOOLS
    for name in PLATFORM_TRANSPARENCY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in PLATFORM_TRANSPARENCY_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == PLATFORM_TRANSPARENCY_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert PLATFORM_TRANSPARENCY_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/osint/providers/osint.platform-transparency.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= PLATFORM_TRANSPARENCY_TOOLS
    for name in ("platform_transparency_ads_by_advertiser", "platform_transparency_ads_for_election"):
        description = tools[name].description.lower()
        assert "as published" in description and ("point estimate" in description or "coordination" in description)
    assert "no user identifiers" in tools["platform_transparency_moderation_statements"].description.lower()


def test_answers_identity_links_monitors_and_bundles_through_mcp_honour_the_decision(mcp_env):
    tools, state = mcp_env
    ads = tools["platform_transparency_ads_by_advertiser"].fn(namespace=h.NS, advertiser=h.PARTY_PAGE)
    assert ads["status"] == "answered" and forbidden_keys(ads) == []
    moderation = tools["platform_transparency_moderation_statements"].fn(
        namespace=h.NS, platform="example-video", start="2099-05-01", end="2099-05-02", include_statements=True)
    assert moderation["statements_counted"] == 6 and "Flagger" not in json.dumps(moderation)
    proposed = tools["propose_platform_transparency_identity_matches"].fn(
        namespace=h.NS, ownership_namespace=h.OWN_NS, campaign_finance_namespace=h.CF_NS,
        lobbying_namespace=h.CF_NS, elections_namespace=h.CF_NS)
    (fund,) = [c for c in proposed["candidates"] if c["method"] == "published-id" and h.FUND in " ".join(c["records"])]
    state["principal"] = "bob"
    reviewed = tools["review_platform_transparency_identity_match"].fn(namespace=h.NS,
                                                                       candidate_id=fund["candidate_id"],
                                                                       decision="accept", reason="FEC id agrees")
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    linked = tools["link_platform_transparency_campaign_finance"].fn(namespace=h.NS,
                                                                      campaign_finance_namespace=h.CF_NS)
    assert linked["status"] == "linked"
    by_committee = tools["platform_transparency_ads_by_advertiser"].fn(namespace=h.NS, advertiser="C00999903")
    assert by_committee["status"] == "answered"
    bundle = tools["export_platform_transparency_evidence_bundle"].fn(namespace=h.NS, query="advertiser",
                                                                      key=h.PARTY_PAGE)
    assert bundle["evidence_bundle"]["bibliography"] and bundle["exclusions"]
    moderation_bundle = tools["export_platform_transparency_evidence_bundle"].fn(
        namespace=h.NS, query="moderation", key="example-video", start="2099-05-01", end="2099-05-02")
    assert len(moderation_bundle["evidence_bundle"]["bibliography"]) == 8  # 2 dump versions and 6 statements
    history = tools["platform_transparency_record_history"].fn(
        namespace=h.NS, record_key="platform-transparency:meta:ad:990000000000101")
    assert history["status"] == "answered" and history["current_as_of"]
    listed = tools["list_platform_transparency_links"].fn(namespace=h.NS, kind="campaign-finance")
    assert listed["links"] and all(link["target_revision"] for link in listed["links"])
    platforms = tools["register_platform_transparency_platforms"].fn(namespace=h.NS)
    assert {p["platform"] for p in platforms["platforms"]} == {"example-market", "example-video", "google", "meta"}
    monitor = tools["create_platform_transparency_monitor"].fn(namespace=h.NS, request_key="mcp",
                                                               watch="platform", key="example-video")
    assert monitor["subscription_id"]
    contracts = tools["platform_transparency_source_contracts"].fn()
    assert contracts["live_verification"]["lumen"]["status"] == "not-implemented"
    assert contracts["minimisation"]["policy"] == "platform-transparency-minimisation-v1"
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_platform_transparency_identity_match"].fn(namespace=h.NS,
                                                                      candidate_id=fund["candidate_id"], reason="x")
    assert refused["ok"] is False


def test_outputs_carrying_withheld_or_point_estimate_fields_are_refused():
    assert guarded({"ads": [{"spend": {"lower_bound": "1", "upper_bound": "9"}}]})
    for bad in ({"ads": [{"spend": {"midpoint": 5}}]}, {"statements": [{"platform_uid": "user-1"}]},
                {"x": {"source_identity": "A notifier"}}):
        with pytest.raises(PlatformTransparencyError) as refused:
            guarded(bad)
        assert refused.value.code == "minimisation_violation"
