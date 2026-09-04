from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.methodology_provenance import (
    EXTRACT_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    MethodologyError,
    MethodologyStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _study(store, external="doi:trial", version="1", predecessor=None, **changes):
    values = {
        "namespace": "scientific",
        "external_id": external,
        "version": version,
        "title": f"Study {external}",
        "design": {"type": "randomized-controlled-trial", "allocation": "parallel"},
        "population": {"condition": "example", "n": 120},
        "interventions": [{"name": "treatment", "dose": "10 mg"}],
        "comparators": [{"name": "placebo"}],
        "outcomes": [{"name": "primary response", "timepoint": "12 weeks"}],
        "datasets": [{"dataset_id": "dataset:trial"}],
        "samples": [{"name": "analysis", "n": 110}],
        "instruments": [{"name": "scale-a"}],
        "analysis_plans": [{"model": "intention-to-treat"}],
        "predecessor_revision_id": predecessor,
        "generation": 2,
        "valid_from_ms": 1,
        "observed_at_ms": 2,
        "producer": {"id": "extractor:v1"},
        "policy": {"id": "methods-policy:v1"},
        "provenance": {"document_id": "paper:1"},
        "principal_id": "curator",
        "scopes": {WRITE_SCOPE},
    }
    values.update(changes)
    return store.register_study(**values)


def _statements():
    return [
        {
            "kind": "sample-size",
            "text": "120 participants",
            "locator": {"page": 4, "section": "Methods"},
            "confidence": 0.94,
        },
        {
            "kind": "sample-size",
            "text": "118 randomized",
            "locator": {"page": 21, "table": "S1"},
            "confidence": 0.72,
            "uncertainty": "supplement conflicts with main text",
        },
        {
            "kind": "analysis",
            "text": "Intention-to-treat",
            "locator": {"page": 5, "passage": "paragraph 2"},
            "confidence": 0.99,
        },
    ]


def test_observational_experimental_schema_evolution_and_missing_fields():
    conn = duckdb.connect(":memory:")
    store = MethodologyStore(conn, now=lambda: 100)
    first = _study(store)
    assert _study(store)["idempotent"]
    second = _study(
        store,
        version="2",
        predecessor=first["study_revision_id"],
        outcomes=[{"name": "primary response"}, {"name": "safety"}],
    )
    assert second["study_id"] == first["study_id"]
    observational = _study(
        store,
        external="doi:cohort",
        design={"type": "observational-cohort"},
        interventions=[],
        comparators=[],
        population={},
    )
    assert observational["population"] == {}
    with pytest.raises(MethodologyError, match="design.type"):
        _study(store, external="bad", design={})
    _validate("noesis-methodology-study-v1.json", second)
    conn.close()


def test_exact_pdf_supplement_locators_conflicts_uncertainty_cancel_and_replay():
    conn = duckdb.connect(":memory:")
    store = MethodologyStore(conn, now=lambda: 100)
    study = _study(store)
    receipt = store.extract(
        "scientific",
        study["study_id"],
        "paper:1",
        _statements(),
        principal_id="extractor",
        scopes={EXTRACT_SCOPE},
    )
    assert (
        receipt["items"][0]["conflict_group"] == receipt["items"][1]["conflict_group"]
    )
    assert receipt["items"][1]["uncertainty"]
    assert store.replay_extraction(
        "scientific", receipt["extraction_id"], scopes={READ_SCOPE}
    )["deterministic"]
    cancelled = store.extract(
        "scientific",
        study["study_id"],
        "paper:2",
        _statements(),
        principal_id="extractor",
        scopes={EXTRACT_SCOPE},
        cancel_requested=True,
    )
    assert cancelled["status"] == "cancelled"
    with pytest.raises(MethodologyError, match="exact locator"):
        store.extract(
            "scientific",
            study["study_id"],
            "paper:bad",
            [{"kind": "sample", "text": "x", "locator": {}, "confidence": 1}],
            principal_id="extractor",
            scopes={EXTRACT_SCOPE},
        )
    _validate("noesis-methodology-extraction-v1.json", receipt)
    conn.close()


def test_frameworks_disagreement_unknown_ratings_revisions_and_citation_closure():
    conn = duckdb.connect(":memory:")
    store = MethodologyStore(conn, now=lambda: 100)
    study = _study(store)
    extracted = store.extract(
        "scientific",
        study["study_id"],
        "paper:1",
        _statements(),
        principal_id="extractor",
        scopes={EXTRACT_SCOPE},
    )
    evidence = [extracted["items"][0]["statement_id"]]
    first = store.assess(
        "scientific",
        study["study_id"],
        "RoB2",
        "selection",
        "low",
        "Random allocation reported",
        evidence_statement_ids=evidence,
        reviewer_id="reviewer:a",
        principal_id="reviewer:a",
        scopes={REVIEW_SCOPE},
    )
    revised = store.assess(
        "scientific",
        study["study_id"],
        "RoB2",
        "selection",
        "high",
        "Allocation concealment unclear",
        evidence_statement_ids=evidence,
        reviewer_id="reviewer:a",
        principal_id="reviewer:a",
        scopes={REVIEW_SCOPE},
    )
    store.assess(
        "scientific",
        study["study_id"],
        "GRADE",
        "external-validity",
        None,
        "Population applicability was not reported",
        source_locator={"document_id": "paper:1", "page": 12},
        principal_id="extractor",
        scopes={REVIEW_SCOPE},
    )
    store.assess(
        "scientific",
        study["study_id"],
        "RoB2",
        "selection",
        "low",
        "Independent review",
        reviewer_id="reviewer:b",
        principal_id="reviewer:b",
        scopes={REVIEW_SCOPE},
    )
    listed = store.limitations("scientific", study["study_id"], scopes={READ_SCOPE})
    assert revised["version"] == first["version"] + 1
    assert any(not item["rating_known"] for item in listed["items"])
    assert listed["reviewer_disagreement"]
    with pytest.raises(MethodologyError, match="unknown statement"):
        store.assess(
            "scientific",
            study["study_id"],
            "RoB2",
            "power",
            None,
            "Unknown",
            evidence_statement_ids=["missing"],
            reviewer_id="r",
            principal_id="r",
            scopes={REVIEW_SCOPE},
        )
    _validate("noesis-methodology-assessment-v1.json", revised)
    conn.close()


def test_artifact_mismatch_version_drift_unavailable_indirect_replication_and_strength():
    conn = duckdb.connect(":memory:")
    store = MethodologyStore(conn, now=lambda: 100)
    study = _study(store)
    mismatch = store.link_artifact(
        "scientific",
        study["study_id"],
        "preregistration",
        "reg:1",
        "registered-as",
        study_external_id="doi:other",
        locator="https://example.test/reg",
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    unavailable = store.link_artifact(
        "scientific",
        study["study_id"],
        "code",
        "repo:gone",
        "analysis-code",
        status="unavailable",
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    replication = store.link_artifact(
        "scientific",
        study["study_id"],
        "replication",
        "doi:replication",
        "indirect-replication",
        version="2",
        indirect_via="method:shared",
        locator="doi:replication",
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    assert mismatch["identifier_mismatch"] and unavailable["status"] == "unavailable"
    assert replication["indirect_via"] == "method:shared"
    with pytest.raises(MethodologyError, match="different version or status"):
        store.link_artifact(
            "scientific",
            study["study_id"],
            "replication",
            "doi:replication",
            "indirect-replication",
            version="3",
            principal_id="curator",
            scopes={WRITE_SCOPE},
        )
    graph = store.replication_graph(
        "scientific", study["study_id"], scopes={READ_SCOPE}
    )
    assert graph["replications"][0]["relation"] == "indirect-replication"
    strength = store.explain_strength(
        "scientific", study["study_id"], scopes={READ_SCOPE}
    )
    assert strength["strength"] == "unknown"
    _validate("noesis-study-artifact-link-v1.json", replication)
    _validate("noesis-methodology-comparison-v1.json", strength)
    conn.close()


def test_comparison_search_namespace_auth_and_social_science_fixture():
    conn = duckdb.connect(":memory:")
    store = MethodologyStore(conn, now=lambda: 100)
    trial = _study(store)
    survey = _study(
        store,
        external="doi:survey",
        design={"type": "cross-sectional-survey"},
        population={"country": "DE"},
        namespace="social-science",
    )
    cohort = _study(
        store, external="doi:cohort", design={"type": "observational-cohort"}
    )
    comparison = store.compare(
        "scientific", [trial["study_id"], cohort["study_id"]], scopes={READ_SCOPE}
    )
    assert any(item["dimension"] == "design" for item in comparison["differences"])
    assert (
        store.search("social-science", "survey", scopes={READ_SCOPE})["items"][0][
            "study_id"
        ]
        == survey["study_id"]
    )
    with pytest.raises(MethodologyError, match="not found"):
        store.study("scientific", survey["study_id"], scopes={READ_SCOPE})
    with pytest.raises(MethodologyError, match="missing required scope"):
        store.search("scientific", "study", scopes={"knowledge:read"})
    _validate("noesis-methodology-comparison-v1.json", comparison)
    conn.close()
