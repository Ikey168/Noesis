from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.ontology import OntologyAlignmentStore, OntologyError
from src.kb.schema_registry import SchemaRegistryError

READ = {"knowledge:schema:read"}
REGISTER = {"knowledge:schema:register"}
VALIDATE = {"knowledge:schema:validate"}
DEPRECATE = {"knowledge:schema:deprecate"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _concept(concept_id, broader=(), **updates):
    value = {
        "concept_id": concept_id,
        "labels": [{"value": concept_id, "language": "en", "kind": "preferred"}],
        "definition": f"Definition of {concept_id}.",
        "broader": list(broader),
        "constraints": {},
        "lifecycle": "active",
    }
    value.update(updates)
    return value


def _publish(store, name, version, concepts, key, **updates):
    values = {
        "owner": "research-team",
        "provenance": {"kind": "user", "source": "offline fixture"},
        "principal_id": "curator",
        "scopes": REGISTER,
        "observed_at_ms": 100,
    }
    values.update(updates)
    return store.publish(name, version, concepts, idempotency_key=key, **values)


def test_immutable_publication_hierarchy_cycles_version_conflicts_and_deprecation():
    conn = duckdb.connect(":memory:")
    store = OntologyAlignmentStore(conn, now=lambda: 100)
    module = _publish(
        store,
        "scientific-domain",
        "1.0.0",
        [_concept("Finding"), _concept("Experiment", ["Finding"])],
        "science-ontology-v1",
    )
    assert (
        module["kind"] == "ontology"
        and module["content"]["contract"] == "noesis-ontology-v1"
    )
    replay = _publish(
        store,
        "scientific-domain",
        "1.0.0",
        [_concept("Finding"), _concept("Experiment", ["Finding"])],
        "science-ontology-v1",
    )
    assert replay["module_id"] == module["module_id"]
    with pytest.raises(SchemaRegistryError, match="silently replaced"):
        _publish(
            store,
            "scientific-domain",
            "1.0.0",
            [_concept("Finding", definition="Changed in-place")],
            "science-ontology-conflict",
        )
    with pytest.raises(OntologyError, match="cycle"):
        _publish(
            store,
            "cyclic-domain",
            "1.0.0",
            [_concept("A", ["B"]), _concept("B", ["A"])],
            "cyclic-ontology-v1",
        )
    deprecated = store.deprecate(
        module["module_id"],
        "Superseded by reviewed terminology.",
        "deprecate-science-v1",
        principal_id="curator",
        scopes=DEPRECATE,
    )
    assert deprecated["status"] == "deprecated"
    with pytest.raises(SchemaRegistryError, match="no active ontology"):
        store.inspect("scientific-domain", "1.0.0", scopes=READ)
    _validate("noesis-ontology-v1.json", module["content"])
    conn.close()


def test_many_to_many_uncertain_local_extension_and_conflicting_crosswalks():
    conn = duckdb.connect(":memory:")
    store = OntologyAlignmentStore(conn, now=lambda: 100)
    _publish(
        store,
        "source-vocab",
        "1.0.0",
        [_concept("Crisis"), _concept("Conflict")],
        "source-vocab-v1",
    )
    _publish(
        store,
        "canonical-vocab",
        "1.0.0",
        [_concept("Emergency"), _concept("ArmedConflict")],
        "canonical-vocab-v1",
    )
    crosswalk = store.register_crosswalk(
        "source-to-canonical",
        "1.0.0",
        {"name": "source-vocab", "version": "1.0.0"},
        {"name": "canonical-vocab", "version": "1.0.0"},
        [
            {
                "kind": "equivalent",
                "source": "Crisis",
                "target": "Emergency",
                "confidence": 0.8,
                "evidence": [{"citation": "map:a"}],
            },
            {
                "kind": "related",
                "source": "Crisis",
                "target": "ArmedConflict",
                "confidence": 0.5,
            },
            {
                "kind": "narrower",
                "source": "Conflict",
                "target": "LocalExtension",
                "confidence": 0.6,
                "local_extension": True,
            },
        ],
        owner="mapping-team",
        provenance={"kind": "imported", "source": "reviewed crosswalk"},
        idempotency_key="source-canonical-crosswalk-v1",
        principal_id="curator",
        scopes=REGISTER,
        observed_at_ms=100,
    )
    assert len(crosswalk["content"]["mappings"]) == 3
    conflicting = store.register_crosswalk(
        "source-to-canonical-conflict",
        "1.0.0",
        {"name": "source-vocab", "version": "1.0.0"},
        {"name": "canonical-vocab", "version": "1.0.0"},
        [
            {
                "kind": "incompatible",
                "source": "Crisis",
                "target": "Emergency",
                "confidence": 0.7,
                "evidence": [{"citation": "map:b"}],
            }
        ],
        owner="mapping-team",
        provenance={"kind": "user", "source": "dissenting review"},
        idempotency_key="conflicting-crosswalk-v1",
        principal_id="curator",
        scopes=REGISTER,
        observed_at_ms=100,
    )
    assert conflicting["module_id"] != crosswalk["module_id"]
    expansion = store.expand(
        {"name": "source-vocab", "version": "1.0.0"},
        "Crisis",
        scopes=READ,
        relationships=["equivalent", "related"],
    )
    assert expansion["conflicts"] == [{"source": "Crisis", "target": "Emergency"}]
    assert "Emergency" not in {item["concept_id"] for item in expansion["terms"]}
    assert "ArmedConflict" in {item["concept_id"] for item in expansion["terms"]}
    _validate("noesis-ontology-crosswalk-v1.json", crosswalk["content"])
    with pytest.raises(OntologyError, match="declared local extension"):
        store.register_crosswalk(
            "bad-extension",
            "1.0.0",
            {"name": "source-vocab", "version": "1.0.0"},
            {"name": "canonical-vocab", "version": "1.0.0"},
            [{"kind": "related", "source": "Crisis", "target": "Missing"}],
            owner="x",
            provenance={"kind": "user", "source": "bad"},
            idempotency_key="bad-extension-key",
            principal_id="curator",
            scopes=REGISTER,
        )
    conn.close()


def test_validation_unknown_partial_constraint_drift_and_idempotent_quarantine():
    conn = duckdb.connect(":memory:")
    store = OntologyAlignmentStore(conn, now=lambda: 100)
    v1 = [
        _concept(
            "Organization",
            constraints={
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        )
    ]
    _publish(store, "entities", "1.0.0", v1, "entities-v1")
    v2 = [
        _concept(
            "Organization",
            constraints={
                "required": ["name", "country"],
                "properties": {
                    "name": {"type": "string"},
                    "country": {"type": "string"},
                },
            },
        )
    ]
    _publish(store, "entities", "1.1.0", v2, "entities-v2")
    valid = store.validate(
        "research",
        "entity:1",
        "entity",
        {"name": "entities", "version": "1.0.0"},
        "Organization",
        {"name": "Acme"},
        source_native={"type": "ORG", "raw": "Acme"},
        quarantine=False,
        principal_id="validator",
        scopes=VALIDATE,
    )
    assert valid["valid"] and valid["source_native"]["type"] == "ORG"
    drifted = store.validate(
        "research",
        "entity:1",
        "entity",
        {"name": "entities", "version": "1.1.0"},
        "Organization",
        {"name": "Acme"},
        source_native={"type": "ORG"},
        quarantine=True,
        principal_id="validator",
        scopes=VALIDATE,
    )
    assert (
        drifted["status"] == "quarantined"
        and drifted["errors"][0]["field"] == "country"
    )
    repeat = store.validate(
        "research",
        "entity:1",
        "entity",
        {"name": "entities", "version": "1.1.0"},
        "Organization",
        {"name": "Acme"},
        source_native={"type": "ORG"},
        quarantine=True,
        principal_id="other",
        scopes=VALIDATE,
    )
    assert repeat["idempotent"] and repeat["quarantine_id"] == drifted["quarantine_id"]
    assert len(store.quarantine("research", scopes=READ)) == 1
    with pytest.raises(OntologyError, match="unknown"):
        store.validate(
            "research",
            "entity:2",
            "entity",
            {"name": "entities", "version": "1.0.0"},
            "Unknown",
            {},
            source_native={},
            quarantine=False,
            principal_id="validator",
            scopes=VALIDATE,
        )
    _validate("noesis-ontology-validation-v1.json", drifted)
    conn.close()


def test_query_expansion_version_pinning_ranking_ambiguity_conflict_and_bounds():
    conn = duckdb.connect(":memory:")
    store = OntologyAlignmentStore(conn, now=lambda: 100)
    _publish(
        store,
        "political",
        "1.0.0",
        [
            _concept("Action"),
            _concept("Sanction", ["Action"]),
            _concept("Embargo", ["Sanction"]),
        ],
        "political-v1",
    )
    _publish(
        store,
        "news",
        "1.0.0",
        [_concept("Restriction"), _concept("Penalty")],
        "news-vocab-v1",
    )
    store.register_crosswalk(
        "political-news",
        "1.0.0",
        {"name": "political", "version": "1.0.0"},
        {"name": "news", "version": "1.0.0"},
        [
            {
                "kind": "equivalent",
                "source": "Sanction",
                "target": "Restriction",
                "confidence": 0.9,
            },
            {
                "kind": "equivalent",
                "source": "Sanction",
                "target": "Penalty",
                "confidence": 0.9,
            },
        ],
        owner="x",
        provenance={"kind": "user", "source": "fixture"},
        idempotency_key="political-news-v1",
        principal_id="curator",
        scopes=REGISTER,
        observed_at_ms=100,
    )
    result = store.expand(
        {"name": "political", "version": "1.0.0"},
        "Sanction",
        scopes=READ,
        relationships=["equivalent", "broader", "narrower"],
        max_depth=2,
        max_terms=10,
    )
    assert (
        result["terms"][0]["concept_id"] == "Sanction"
        and result["terms"][0]["score"] == 1
    )
    mapped = [item for item in result["terms"] if item["ontology"] == "news"]
    assert {item["concept_id"] for item in mapped} == {"Restriction", "Penalty"}
    assert all(item["path"][0]["crosswalk_module_id"] for item in mapped)
    assert result["ambiguous"]
    assert {item["ontology"] for item in result["aggregations"]} == {
        "political",
        "news",
    }
    bounded = store.expand(
        {"name": "political", "version": "1.0.0"},
        "Action",
        scopes=READ,
        relationships=["narrower"],
        max_depth=6,
        max_terms=2,
    )
    assert len(bounded["terms"]) == 2 and bounded["truncated"]
    _validate("noesis-ontology-expansion-v1.json", result)
    conn.close()


def test_version_diff_deterministic_export_and_six_domain_offline_fixtures():
    conn = duckdb.connect(":memory:")
    store = OntologyAlignmentStore(conn, now=lambda: 100)
    domains = ["research", "political", "economic", "osint", "technical", "scientific"]
    for domain in domains:
        _publish(
            store,
            f"{domain}-fixture",
            "1.0.0",
            [_concept("Record")],
            f"{domain}-ontology-v1",
        )
    _publish(
        store,
        "research-fixture",
        "1.1.0",
        [
            _concept("Record", labels=[{"value": "Research record", "language": "en"}]),
            _concept("Finding", ["Record"]),
        ],
        "research-ontology-v2",
    )
    diff = store.diff("research-fixture", "1.0.0", "1.1.0", scopes=READ)
    assert diff["added"] == ["Finding"] and diff["changed"][0]["fields"] == ["labels"]
    first = store.export(scopes=READ)
    second = store.export(scopes=READ)
    assert first == second
    assert {module["name"] for module in first["modules"]} >= {
        f"{domain}-fixture" for domain in domains
    }
    _validate("noesis-ontology-export-v1.json", first)
    with pytest.raises(OntologyError, match="at least one concept"):
        _publish(store, "malformed", "1.0.0", [], "malformed-ontology-key")
    with pytest.raises(OntologyError, match="required scope"):
        store.export(scopes=set())
    conn.close()
