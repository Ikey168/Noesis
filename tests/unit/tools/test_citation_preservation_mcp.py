from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_citation_mcp_policy_capture_verify_repair_export_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "citations.duckdb"
    scopes = {"knowledge:citation:read"}
    monkeypatch.setattr(server, "_context", lambda: ("researcher", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_citation_archive_policy",
        "get_citation_archive_policy",
        "capture_citation_snapshot",
        "get_citation_snapshot",
        "replay_citation_snapshot",
        "verify_preserved_citation",
        "record_citation_health",
        "get_citation_status",
        "preview_citation_repair",
        "accept_citation_repair",
        "export_preserved_citations",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_citation_archive_policy"],
        namespace="scientific",
        policy_id="p",
        version="1",
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:citation:write")
    policy = _call(
        tools["register_citation_archive_policy"],
        namespace="scientific",
        policy_id="p",
        version="1",
        allowed_licenses=["CC"],
        approved_archives=["https://archive.test"],
    )
    denied_capture = _call(
        tools["capture_citation_snapshot"],
        namespace="scientific",
        policy_id="p",
        citation_id="c",
        source_url="https://source",
        content="claim evidence",
        license_id="CC",
    )
    assert denied_capture["error"]["code"] == "unauthorized"
    scopes.add("knowledge:citation:capture")
    snapshot = _call(
        tools["capture_citation_snapshot"],
        namespace="scientific",
        policy_id="p",
        citation_id="c",
        source_url="https://source",
        content="claim evidence",
        license_id="CC",
        locator={"page": 1},
    )
    assert _call(
        tools["replay_citation_snapshot"],
        namespace="scientific",
        snapshot_id=snapshot["snapshot_id"],
    )["deterministic"]
    verification = _call(
        tools["verify_preserved_citation"],
        namespace="scientific",
        citation_id="c",
        snapshot_id=snapshot["snapshot_id"],
        assertion="claim evidence",
        expected_excerpt="claim evidence",
    )
    assert verification["status"] == "supports"
    _call(
        tools["record_citation_health"],
        namespace="scientific",
        citation_id="c",
        url="https://source",
        http_status=404,
    )
    preview = _call(
        tools["preview_citation_repair"],
        namespace="scientific",
        policy_id=policy["policy_id"],
        citation_id="c",
        snapshot_id=snapshot["snapshot_id"],
        candidates=[
            {
                "archive": "https://archive.test",
                "url": "https://archive.test/c",
                "content_hash": snapshot["blob_hash"],
            }
        ],
    )
    assert preview["candidates"][0]["eligible"]
    denied_repair = _call(
        tools["accept_citation_repair"],
        namespace="scientific",
        preview=preview,
        candidate_index=0,
    )
    assert denied_repair["error"]["code"] == "unauthorized"
    scopes.add("knowledge:citation:repair")
    repair = _call(
        tools["accept_citation_repair"],
        namespace="scientific",
        preview=preview,
        candidate_index=0,
    )
    assert repair["original_unchanged"]
    exported = _call(
        tools["export_preserved_citations"], namespace="scientific", citation_ids=["c"]
    )
    assert exported["dependency_complete"]


def test_citation_catalog_scopes_and_capabilities():
    assert _mutability("capture_citation_snapshot") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "capture_citation_snapshot"
    ) == ["knowledge:citation:capture"]
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "accept_citation_repair"
    ) == ["knowledge:citation:repair"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "export_preserved_citations"
    ) == ["knowledge:citation:read"]
    capabilities = server.knowledge_engine_capabilities.fn()
    assert (
        "noesis-citation-snapshot-v1" in capabilities["contracts"]
        and "approved-archive-link-rot-repair" in capabilities["features"]
    )
