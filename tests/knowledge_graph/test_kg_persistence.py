"""Durability and standalone read-only KG MCP regression tests (#1006)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from src.knowledge_graph.foundation import (
    DuckDBKnowledgeGraphStore,
    EntityType,
    Node,
    Provenance,
    RelationType,
    Triple,
)


def _seed(path: Path) -> tuple[str, str]:
    store = DuckDBKnowledgeGraphStore(path)
    document = store.add_node(Node(type=EntityType.DOCUMENT, name="durable-doc"))
    entity = store.add_node(Node(type=EntityType.ORGANIZATION, name="Durable Labs"))
    store.add_triple(Triple(
        document.node_id, RelationType.MENTIONS, entity.node_id,
        Provenance(source_doc="durable-doc", confidence=0.9, extractor="test"),
    ))
    store.record_event("triple", "|".join((document.node_id, "MENTIONS", entity.node_id)),
                       "MENTIONS:Durable Labs", "durable-doc")
    store.close()
    return document.node_id, entity.node_id


def test_graph_survives_reopen_and_read_only_rejects_writes(tmp_path):
    path = tmp_path / "warehouse.duckdb"
    _seed(path)
    reopened = DuckDBKnowledgeGraphStore(path, read_only=True)
    assert reopened.node_count == 2
    assert reopened.triple_count == 1
    with pytest.raises(PermissionError):
        reopened.add_node(Node(type=EntityType.CONCEPT, name="No mutation"))
    reopened.close()


def test_standalone_kg_mcp_process_reads_seeded_graph(tmp_path):
    path = tmp_path / "warehouse.duckdb"
    _seed(path)
    code = (
        "import json; "
        "from tools.kg_mcp.server import kg_stats, list_entities; "
        "print(json.dumps({'stats': kg_stats.fn(), 'entities': list_entities.fn()}))"
    )
    env = dict(os.environ, NOESIS_DB_PATH=str(path), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["stats"]["node_count"] == 2
    assert payload["stats"]["triple_count"] == 1
    assert payload["stats"]["read_only"] is True
    assert any(row["name"] == "Durable Labs" for row in payload["entities"])
