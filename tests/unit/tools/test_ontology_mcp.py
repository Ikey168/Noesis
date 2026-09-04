from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.schema_registry_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _concept(concept_id, broader=None, constraints=None):
    return {
        "concept_id": concept_id,
        "labels": [{"value": concept_id, "language": "en"}],
        "definition": f"Definition of {concept_id}.",
        "broader": broader or [],
        "constraints": constraints or {},
        "lifecycle": "active",
    }


def test_ontology_mcp_publication_mapping_validation_expansion_export_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "ontology.duckdb"
    scopes = {"knowledge:schema:read"}
    monkeypatch.setattr(server, "_context", lambda: ("curator", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_ontology",
        "inspect_ontology",
        "deprecate_ontology",
        "register_ontology_crosswalk",
        "validate_knowledge_object_ontology",
        "list_ontology_quarantine",
        "expand_ontology_query",
        "diff_ontology_versions",
        "export_ontology_alignment",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_ontology"],
        name="technical-fixture",
        semantic_version="1.0.0",
        concepts=[_concept("System")],
        owner="team",
        provenance={"kind": "user", "source": "fixture"},
        idempotency_key="technical-ontology-v1",
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.update(
        {
            "knowledge:schema:register",
            "knowledge:schema:validate",
            "knowledge:schema:deprecate",
        }
    )
    source = _call(
        tools["register_ontology"],
        name="technical-fixture",
        semantic_version="1.0.0",
        concepts=[
            _concept("System"),
            _concept(
                "Service",
                ["System"],
                {"required": ["name"], "properties": {"name": {"type": "string"}}},
            ),
        ],
        owner="team",
        provenance={"kind": "user", "source": "fixture"},
        idempotency_key="technical-ontology-v1",
        observed_at_ms=100,
    )
    _call(
        tools["register_ontology"],
        name="canonical-fixture",
        semantic_version="1.0.0",
        concepts=[_concept("SoftwareService")],
        owner="team",
        provenance={"kind": "user", "source": "fixture"},
        idempotency_key="canonical-ontology-v1",
        observed_at_ms=100,
    )
    crosswalk = _call(
        tools["register_ontology_crosswalk"],
        name="technical-canonical-map",
        semantic_version="1.0.0",
        source={"name": "technical-fixture", "version": "1.0.0"},
        target={"name": "canonical-fixture", "version": "1.0.0"},
        mappings=[
            {
                "kind": "equivalent",
                "source": "Service",
                "target": "SoftwareService",
                "confidence": 0.9,
                "evidence": [{"citation": "review:1"}],
            }
        ],
        owner="team",
        provenance={"kind": "user", "source": "fixture"},
        idempotency_key="technical-map-v1",
        observed_at_ms=100,
    )
    assert crosswalk["kind"] == "crosswalk"
    invalid = _call(
        tools["validate_knowledge_object_ontology"],
        namespace="technical",
        object_id="service:1",
        object_kind="entity",
        ontology={"name": "technical-fixture", "version": "1.0.0"},
        concept_id="Service",
        value={},
        source_native={"class": "svc"},
        quarantine=True,
    )
    assert invalid["status"] == "quarantined"
    expanded = _call(
        tools["expand_ontology_query"],
        ontology={"name": "technical-fixture", "version": "1.0.0"},
        concept_id="Service",
        relationships=["equivalent", "broader"],
    )
    assert "SoftwareService" in {item["concept_id"] for item in expanded["terms"]}
    exported = _call(tools["export_ontology_alignment"])
    assert source["module_id"] in {item["module_id"] for item in exported["modules"]}


def test_ontology_catalog_mutability_and_scopes():
    for name in ("register_ontology", "register_ontology_crosswalk"):
        assert _mutability(name) == "write"
        assert _required_scopes("schema_registry_mcp", "write", name) == [
            "knowledge:schema:register"
        ]
    assert _mutability("deprecate_ontology") == "write"
    assert _required_scopes("schema_registry_mcp", "write", "deprecate_ontology") == [
        "knowledge:schema:deprecate"
    ]
    assert _mutability("validate_knowledge_object_ontology") == "write"
    assert _required_scopes(
        "schema_registry_mcp", "write", "validate_knowledge_object_ontology"
    ) == ["knowledge:schema:validate"]
    for name in (
        "inspect_ontology",
        "list_ontology_quarantine",
        "expand_ontology_query",
        "diff_ontology_versions",
        "export_ontology_alignment",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("schema_registry_mcp", "read", name) == [
            "knowledge:schema:read"
        ]
