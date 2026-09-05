import asyncio
import json
from pathlib import Path

import duckdb

from src.ingestion.opencitations import OpenCitationsClient
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server as writer
from tools.research_mcp import server as reader


def test_capture_resume_and_research_traversal_tools(tmp_path, monkeypatch):
    fixture = json.loads(
        Path("tests/fixtures/integrations/opencitations-native.json").read_text()
    )
    db = tmp_path / "research.duckdb"
    scopes = {"knowledge:read"}
    monkeypatch.setattr(writer, "_context", lambda: ("researcher", scopes))
    monkeypatch.setattr(
        writer, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    monkeypatch.setattr(
        reader, "_warehouse_ro", lambda: duckdb.connect(str(db), read_only=True)
    )
    monkeypatch.setattr(
        OpenCitationsClient, "snapshot", lambda self, *args, **kwargs: fixture
    )
    tools = asyncio.run(writer.mcp.get_tools())
    acquire = tools["acquire_opencitations"].fn
    assert acquire(fixture["identifier"])["error"]["code"] == "unauthorized"
    scopes.add("knowledge:citation:capture")
    first = acquire(fixture["identifier"], page_size=3)
    assert first["imported"] == 3
    read_tools = asyncio.run(reader.mcp.get_tools())
    traverse = read_tools["citation_graph"].fn
    assert (
        traverse(identifier=fixture["identifier"], direction="references")["edge_count"]
        == 3
    )
    assert (
        acquire(
            fixture["identifier"],
            snapshot_sha256=first["snapshot_sha256"],
            cursor=first["next_cursor"],
        )["next_cursor"]
        is None
    )
    graph = traverse(
        identifier=fixture["identifier"], direction="references", limit=100
    )
    assert graph["edge_count"] == 72
    assert all(e["independent_evidence_count"] is None for e in graph["edges"])
    assert "error" in traverse(identifier="invalid")
    assert _mutability("acquire_opencitations") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "acquire_opencitations"
    ) == ["knowledge:citation:capture"]
