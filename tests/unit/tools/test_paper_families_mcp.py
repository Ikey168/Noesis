"""Public paper-family tools over generated, retained scholarly fixtures."""

import asyncio
import json
from pathlib import Path

import duckdb
import jsonschema

from src.ingestion.revisions import DocumentRevisionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_public_paper_family_workflow_and_schemas(tmp_path, monkeypatch):
    path = tmp_path / "scholarly.duckdb"
    conn = duckdb.connect(str(path))
    record = DocumentRevisionStore(conn).observe({
        "document_id": "preprint", "title": "A retained preprint",
        "content": "Generated abstract", "authors": ["A. Author"],
        "metadata": {"content_representation": "plain-text-abstract"},
    })
    conn.close()
    scopes = {"knowledge:paper-family:read", "knowledge:paper-family:write",
              "namespace:research:read", "namespace:research:write", "document:preprint:read"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(str(path), read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    created = tools["create_paper_family"].fn(
        namespace="research", family_key="fixture", root_member={
            "document_id": "preprint", "revision_id": record["revision_id"],
            "stage": "preprint", "identifiers": [{"kind": "arxiv", "value": "2501.00001v1"}],
        },
    )
    assert created["contract"] == "noesis-paper-family-v1", created
    family_id = created["family_id"]
    inspected = tools["inspect_paper_family"].fn(namespace="research", family_id=family_id)
    assert inspected["members"][0]["availability"] == "abstract-only"
    selected = tools["select_paper_family_citation"].fn(
        namespace="research", family_id=family_id, command_key="cite",
        member_id=inspected["members"][0]["member_id"], expected_revision=1,
        locator={"section": "Abstract"},
    )
    assert selected["revision"] == 2, selected
    exported = tools["export_paper_family"].fn(namespace="research", family_id=family_id)
    assert exported["bibliography"][0]["source"]["revision_id"] == record["revision_id"]
    schemas = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"
    for value, name in [(selected, "noesis-paper-family-v1.json"),
                        (exported, "noesis-paper-family-export-v1.json")]:
        jsonschema.validate(value, json.loads((schemas / name).read_text()))
    assert _mutability("select_paper_family_citation") == "write"
    assert _mutability("export_paper_family") == "read"
    assert _required_scopes("knowledge_engine_mcp", "read", "export_paper_family") == ["knowledge:paper-family:read"]
    scopes.remove("document:preprint:read")
    denied = tools["inspect_paper_family"].fn(namespace="research", family_id=family_id)
    assert denied["error"]["code"] == "unauthorized"
