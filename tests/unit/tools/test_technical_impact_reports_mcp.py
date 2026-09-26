"""Public technical tools over generated package and advisory records."""

import asyncio
import json
from pathlib import Path

import duckdb
import jsonschema

from src.domains.technical.model import package_object_id, record_advisory_range, record_object
from src.ingestion.revisions import DocumentRevisionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _advisory(conn, advisory_id, package_id, introduced, fixed):
    DocumentRevisionStore(conn).observe({"document_id": "osv:" + advisory_id,
                                         "content": "Generated advisory " + advisory_id})
    record_object(conn, object_type="advisory", object_id=advisory_id,
                  canonical_name=advisory_id, source_document_id="osv:" + advisory_id,
                  status="active", observed_at=2)
    record_advisory_range(conn, advisory_id, package_id, ecosystem="pypi",
                          range_type="ECOSYSTEM", events=[{"introduced": introduced}, {"fixed": fixed}],
                          source_document_id="osv:" + advisory_id, observed_at=2)


def test_public_report_new_advisory_fixed_dependency_and_unchanged_inventory(tmp_path, monkeypatch):
    path = tmp_path / "technical-reports.duckdb"
    with duckdb.connect(str(path)) as conn:
        revisions = DocumentRevisionStore(conn)
        for name in ("affected", "fixed"):
            revisions.observe({"document_id": "pypi:" + name, "content": "Generated package " + name})
            coordinate = "pkg:pypi:" + name
            record_object(conn, object_type="package", object_id=package_object_id(coordinate),
                          coordinate=coordinate, canonical_name=name,
                          source_document_id="pypi:" + name, observed_at=1)
        _advisory(conn, "A-old", package_object_id("pkg:pypi:affected"), "1.0", "2.0")
        _advisory(conn, "F-fixed", package_object_id("pkg:pypi:fixed"), "1.0", "2.0")
    scopes = {"knowledge:technical:read", "knowledge:technical:write",
              "namespace:research:read", "namespace:research:write",
              "document:pypi:affected:read", "document:pypi:fixed:read",
              "document:osv:A-old:read", "document:osv:F-fixed:read"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(str(path), read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    inventory = tools["import_technical_inventory"].fn(
        content="affected==1.5\nfixed==2.0\n", format="requirements.txt")
    first = tools["create_technical_impact_report"].fn(
        namespace="research", request_key="first", inventory_id=inventory["inventory_id"])
    assert [f["classification"] for f in first["findings"]] == [
        "affected", "unaffected_under_assessed_ranges"]
    jsonschema.validate(first, json.loads((SCHEMAS / "noesis-technical-impact-report-v1.json").read_text()))
    with duckdb.connect(str(path)) as conn:
        _advisory(conn, "A-new", package_object_id("pkg:pypi:fixed"), "2.0", "3.0")
    scopes.add("document:osv:A-new:read")
    second = tools["create_technical_impact_report"].fn(
        namespace="research", request_key="second", inventory_id=inventory["inventory_id"])
    third = tools["create_technical_impact_report"].fn(
        namespace="research", request_key="third", inventory_id=inventory["inventory_id"])
    changed = tools["compare_technical_impact_reports"].fn(
        namespace="research", left_report_id=first["report_id"], right_report_id=second["report_id"])
    unchanged = tools["compare_technical_impact_reports"].fn(
        namespace="research", left_report_id=second["report_id"], right_report_id=third["report_id"])
    assert changed["same_inventory"] and changed["changed_count"] == 1
    assert unchanged["same_inventory"] and unchanged["same_sources"] and unchanged["changed_count"] == 0
    exported = tools["export_technical_impact_report"].fn(
        namespace="research", report_id=first["report_id"])
    assert exported["bibliography"] and not exported["executed_changes"]
    jsonschema.validate(exported, json.loads((SCHEMAS / "noesis-technical-impact-export-v1.json").read_text()))
    assert _mutability("create_technical_impact_report") == "write"
    assert _mutability("export_technical_impact_report") == "read"
    assert _required_scopes("knowledge_engine_mcp", "read", "export_technical_impact_report") == ["knowledge:technical:read"]
    scopes.remove("document:osv:A-old:read")
    denied = tools["inspect_technical_impact_report"].fn(namespace="research", report_id=first["report_id"])
    assert denied["error"]["code"] == "unauthorized"
