"""Web-archive MCP entry points: registration, scopes, resolve, as-of, pin and the gated Save Page Now (#2330)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingestion import memento, wayback
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import web_archive_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.web_archives import WEB_ARCHIVE_SCOPES, WEB_ARCHIVE_TOOLS, WEB_ARCHIVE_WRITES
from src.mcp_host.introspection import tool_map


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "web-archives-mcp.duckdb")
    duckdb.connect(path).close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    transport = h.FakeArchives()
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    monkeypatch.setattr(memento, "_request", transport)

    def refuse(**_):
        raise AssertionError("Save Page Now must never reach the network in tests")

    monkeypatch.setattr(wayback, "_spn_request", refuse)
    return tool_map(server.mcp), state, transport


def test_tools_are_registered_with_their_scopes_and_catalogued(mcp_env):
    tools, _, _ = mcp_env
    assert WEB_ARCHIVE_TOOLS <= set(tools) and set(WEB_ARCHIVE_SCOPES) == WEB_ARCHIVE_TOOLS
    for name in WEB_ARCHIVE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in WEB_ARCHIVE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == WEB_ARCHIVE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert WEB_ARCHIVE_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/platform/providers/platform.web-archives.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == WEB_ARCHIVE_TOOLS
    for op in descriptor["operations"]:
        assert op["required_scopes"] == WEB_ARCHIVE_SCOPES[op["tool"].split(".", 1)[1]], op["id"]
    assert "never" in tools["resolve_web_archive_captures"].description


def test_resolve_answer_match_and_pin_through_mcp(mcp_env):
    tools, state, transport = mcp_env
    resolved = tools["resolve_web_archive_captures"].fn(namespace=h.NS, url=h.URL, request_id="mcp-1",
                                                        crawls=[h.CRAWL])
    assert resolved["archives"]["internet-archive"]["outcome"] == "captures", resolved
    assert {c["url"].split("/")[2] for c in transport.calls} == {"timetravel.mementoweb.org", "index.commoncrawl.org"}
    answer = tools["web_page_as_of"].fn(namespace=h.NS, url=h.URL, at="2024-02-20")
    assert answer["status"] == "answered" and answer["closest"]["archive_id"] == "internet-archive"
    listed = tools["list_web_archive_captures"].fn(namespace=h.NS, url=h.URL)
    assert listed["captures"] and listed["timemaps"]
    proposed = tools["propose_web_archive_matches"].fn(namespace=h.NS, citation_id="cite:mcp", cited_url=h.CITED)
    match = proposed["matches"][0]
    state["principal"] = "bob"
    reviewed = tools["review_web_archive_match"].fn(namespace=h.NS, match_id=match["match_id"], decision="accept",
                                                    reason="same page")
    assert reviewed["state"] == "accepted"
    pin = tools["pin_citation_capture"].fn(namespace=h.NS, citation_id="cite:mcp", capture_id=match["capture_id"],
                                           cited_url=h.CITED)
    assert pin["cited_url"] == h.CITED and pin["revision"] == 1
    pins = tools["list_citation_pins"].fn(namespace=h.NS, citation_id="cite:mcp")
    assert pins["pins"][0]["pin_id"] == pin["pin_id"] and pins["matches"]
    contracts = tools["web_archive_contracts"].fn()
    assert contracts["archives"]["archive-today"]["access_decision"] == "excluded"
    assert tools["web_archive_readiness"].fn()["save_page_now_feature"] is False


def test_save_page_now_is_refused_without_its_scope_or_feature(mcp_env):
    tools, state, _ = mcp_env
    denied = tools["request_save_page_now"].fn(namespace=h.NS, url="https://example.com/", request_id="spn")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.ARCHIVE_WRITE)
    off = tools["request_save_page_now"].fn(namespace=h.NS, url="https://example.com/", request_id="spn")
    assert off["ok"] is False and off["error"]["code"] == "feature_disabled"
    assert tools["check_save_page_now"].fn(namespace=h.NS, request_id="spn")["error"]["code"] == "feature_disabled"
    state["scopes"] = set(h.READ_ONLY)
    assert tools["pin_citation_capture"].fn(namespace=h.NS, citation_id="c", capture_id="x",
                                            cited_url=h.URL)["ok"] is False
