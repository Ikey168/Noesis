import asyncio

import duckdb

from tests.unit.kb.test_event_dossiers import seed
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.event_dossiers import DOSSIER_READS, DOSSIER_WRITES


def test_public_event_dossier_workflow_with_revocation(tmp_path, monkeypatch):
    path = str(tmp_path / "dossiers.duckdb")
    conn = duckdb.connect(path)
    _, event, _, sources = seed(conn)
    conn.close()
    scopes = {"operator"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())

    def call(name, **kwargs):
        result = tools[name].fn(**kwargs)
        assert "error" not in result, result
        return result

    first = call(
        "create_event_dossier",
        namespace="osint",
        request_key="first",
        event_id=event["event_id"],
    )
    assert (
        call(
            "inspect_event_dossier", namespace="osint", dossier_id=first["dossier_id"]
        )["event_id"]
        == event["event_id"]
    )
    second = call(
        "revise_event_dossier",
        namespace="osint",
        dossier_id=first["dossier_id"],
        expected_revision=1,
        overrides=[
            {
                "document_revision_id": sources["copy"],
                "decision": "exclude",
                "rationale": "Syndicated copy",
                "reviewed_by": "alice",
            }
        ],
    )
    assert second["revision"] == 2
    assert call(
        "event_dossier_timeline", namespace="osint", dossier_id=first["dossier_id"]
    )["items"]
    comparison = call(
        "compare_event_dossier_revisions",
        namespace="osint",
        dossier_id=first["dossier_id"],
        from_revision=1,
        to_revision=2,
    )
    assert comparison["source_changes"]
    exported = call(
        "export_event_dossier_comparison",
        namespace="osint",
        dossier_id=first["dossier_id"],
        from_revision=1,
        to_revision=2,
    )
    assert sources["copy"] in exported["markdown"]
    report = call(
        "create_event_dossier_report",
        namespace="osint",
        request_key="report",
        dossier_id=first["dossier_id"],
        revision=1,
    )
    assert report["subscription_operations"][0] == "subscribe_cited_evidence"
    scopes.clear()
    scopes.update(
        {"knowledge:event-dossier:read", "knowledge:event:read", "namespace:osint:read"}
    )
    denied = tools["inspect_event_dossier"].fn(
        namespace="osint", dossier_id=first["dossier_id"]
    )
    assert denied["error"]["code"] == "unauthorized"
    for name in DOSSIER_WRITES:
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:event-dossier:write",
            "knowledge:event:write",
        ]
    for name in DOSSIER_READS:
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:event-dossier:read",
            "knowledge:event:read",
        ]
