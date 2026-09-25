import asyncio

import duckdb

from services.ingest.common.series_model import SeriesRecord
from src.domains.economic.model import register_series
from src.ingestion.revisions import DocumentRevisionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.economic_releases import (
    ECONOMIC_RELEASE_READS,
    ECONOMIC_RELEASE_WRITES,
)


def test_public_two_release_report_and_revocation(tmp_path, monkeypatch):
    path = str(tmp_path / "economic.duckdb")
    conn = duckdb.connect(path)
    revisions = DocumentRevisionStore(conn)
    source_revisions = {}
    for as_of, value, document_id in [
        (1000, 1.0, "release-old"),
        (2000, 2.0, "release-new"),
    ]:
        source_revisions[document_id] = revisions.observe(
            {
                "document_id": document_id,
                "content": f"Reported value {value}",
                "ingested_at": as_of,
            }
        )["revision_id"]
        register_series(
            conn,
            SeriesRecord(
                series_id="fixture:economy",
                provider="fixture",
                title="Fixture economy",
                frequency="annual",
                unit="count",
                geography="US",
                as_of=as_of,
                observations=[{"period": "2025", "value": value}],
                metadata={"release_at": as_of, "retrieved_at": as_of},
            ),
            semantics={
                "indicator_id": "metric:fixture",
                "scaling": 1,
                "source_document_id": document_id,
            },
        )
    conn.close()
    scopes = {"operator"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())

    def call(name, **arguments):
        result = tools[name].fn(**arguments)
        assert "error" not in result, result
        return result

    selected_old = [
        {
            "series_id": "fixture:economy",
            "provider_release_id": "fixture-release-old",
            "source_revision_id": source_revisions["release-old"],
        }
    ]
    selected_new = [
        {
            "series_id": "fixture:economy",
            "provider_release_id": "fixture-release-new",
            "source_revision_id": source_revisions["release-new"],
        }
    ]
    old = call(
        "create_economic_release_snapshot",
        namespace="economics",
        request_key="old",
        release_id="old",
        release_cutoff_ms=1000,
        acquired_cutoff_ms=1000,
        series=selected_old,
    )
    new = call(
        "create_economic_release_snapshot",
        namespace="economics",
        request_key="new",
        release_id="new",
        release_cutoff_ms=2000,
        acquired_cutoff_ms=2000,
        series=selected_new,
    )
    unavailable = call(
        "create_economic_release_snapshot",
        namespace="economics",
        request_key="late",
        release_id="late",
        release_cutoff_ms=2000,
        acquired_cutoff_ms=1000,
        series=[{"series_id": "fixture:economy", "vintage_id": "fixture:economy@2000"}],
    )
    assert unavailable["series"][0]["unavailable_reason"] == "late_acquired"
    comparison = call(
        "compare_economic_release_snapshots",
        namespace="economics",
        request_key="comparison",
        left_snapshot_id=old["snapshot_id"],
        right_snapshot_id=new["snapshot_id"],
        assumptions=["Same methodology and unit"],
    )
    assert len(comparison["items"][0]["same_period_revisions"]) == 1
    exported = call(
        "export_economic_release_comparison",
        namespace="economics",
        comparison_id=comparison["comparison_id"],
    )
    assert "source:" in exported["markdown"]
    assert "economic-calculation:" in exported["markdown"]
    report = call(
        "create_economic_comparison_report",
        namespace="economics",
        request_key="authored",
        comparison_id=comparison["comparison_id"],
    )["report"]
    before = call(
        "export_authored_report",
        namespace="economics",
        report_id=report["report_id"],
        revision=1,
    )
    assessed = call(
        "assess_authored_report_changes",
        namespace="economics",
        report_id=report["report_id"],
    )
    assert assessed["sections"]
    conn = duckdb.connect(path)
    DocumentRevisionStore(conn).observe(
        {"document_id": "release-new", "content": "Revised source", "ingested_at": 3000}
    )
    conn.close()
    affected = call(
        "assess_authored_report_changes",
        namespace="economics",
        report_id=report["report_id"],
    )
    assert affected["sections"][0]["status"] == "affected"
    assert (
        call(
            "export_authored_report",
            namespace="economics",
            report_id=report["report_id"],
            revision=1,
        )
        == before
    )
    scopes.clear()
    scopes.update({"knowledge:economic:read", "namespace:economics:read"})
    denied_source = tools["inspect_economic_release_snapshot"].fn(
        namespace="economics", snapshot_id=old["snapshot_id"]
    )
    assert denied_source["error"]["code"] == "unauthorized"
    scopes.clear()
    denied = tools["inspect_economic_release_snapshot"].fn(
        namespace="economics", snapshot_id=old["snapshot_id"]
    )
    assert denied["error"]["code"] == "unauthorized"
    for name in ECONOMIC_RELEASE_WRITES:
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:economic:write"
        ]
    for name in ECONOMIC_RELEASE_READS:
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:economic:read"
        ]
