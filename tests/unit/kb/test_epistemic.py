from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.epistemic import (
    EpistemicError,
    EpistemicStore,
    aggregate_evidence,
    classify_statement,
)

READ = {"knowledge:epistemic:read"}
WRITE = {"knowledge:epistemic:write"}
REVIEW = {"knowledge:epistemic:review"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("The agency allegedly withheld the report.", "allegation"),
        ("Inflation is forecast to fall next year.", "forecast"),
        ("Approximately 40 percent responded.", "estimate"),
        ("The ministry should publish the data.", "normative"),
        ("We propose that the effect may explain the result.", "hypothesis"),
        ("According to the filing, revenue rose.", "report"),
        ("Revenue rose in 2025.", "fact"),
    ],
)
def test_deterministic_taxonomy_classification(text, status):
    assert classify_statement(text)["status"] == status


def test_pinned_classifier_and_offline_fallback_provenance():
    model = classify_statement(
        "Some statement",
        classifier=lambda _: {"status": "opinion", "confidence": 0.9},
        classifier_pin={"name": "classifier", "version": "2", "revision": "abc"},
    )
    assert model["classifier"]["revision"] == "abc"
    assert model["rule_fallback"]["status"] == "fact"
    with pytest.raises(EpistemicError, match="name, version, and revision"):
        classify_statement("Some statement", classifier=lambda _: {"status": "fact"})


def test_independence_aware_evidence_aggregation_and_contestation():
    evidence = [
        {
            "source_id": "wire-a",
            "independence_group": "wire",
            "stance": "support",
            "reliability": 0.9,
        },
        {
            "source_id": "copy-b",
            "independence_group": "wire",
            "stance": "support",
            "reliability": 0.8,
        },
        {
            "source_id": "primary",
            "independence_group": "primary",
            "stance": "contradict",
            "reliability": 0.7,
        },
    ]
    result = aggregate_evidence(evidence)
    assert result["assessment_state"] == "contested"
    assert len(result["independent_groups"]) == 2
    assert (
        next(item for item in result["independent_groups"] if item["group"] == "wire")[
            "members"
        ]
        == 2
    )


def test_missing_evidence_and_calibration_boundaries_are_explicit():
    assert aggregate_evidence([])["assessment_state"] == "insufficient"
    boundary = aggregate_evidence(
        [
            {
                "source_id": "primary",
                "stance": "support",
                "reliability": 0.65,
                "freshness": 1.0,
                "methodology": 1.0,
            }
        ]
    )
    assert boundary["assessment_state"] == "supported"
    assert boundary["confidence"] < 1.0
    with pytest.raises(EpistemicError, match="finite number"):
        aggregate_evidence(
            [{"source_id": "bad", "stance": "support", "reliability": 1.1}]
        )


def test_versioned_assessment_review_override_history_and_filters():
    conn = duckdb.connect(":memory:")
    store = EpistemicStore(conn, now=lambda: 100)
    evidence = [{"source_id": "primary", "stance": "support", "reliability": 1.0}]
    first = store.assess(
        "research",
        "claim-1",
        "The result was replicated.",
        evidence,
        principal_id="service",
        scopes=WRITE,
        source_revision_id="derived-revision:1",
    )
    assert first["machine_status"] == "fact"
    reviewed = store.override(
        "research",
        "claim-1",
        "report",
        "The sentence attributes an external result.",
        reviewer_id="reviewer",
        scopes=REVIEW,
    )
    assert (
        reviewed["machine_status"] == "fact"
        and reviewed["effective_status"] == "report"
    )
    second = store.assess(
        "research",
        "claim-1",
        "The result was reportedly replicated.",
        evidence,
        principal_id="service",
        scopes=WRITE,
        source_revision_id="derived-revision:2",
    )
    assert second["revision"] == 2 and second["machine_status"] == "report"
    history = store.get("research", "claim-1", scopes=READ, include_history=True)
    assert [item["revision"] for item in history["revisions"]] == [1, 2]
    assert (
        store.search("research", scopes=READ, statuses=["report"])[0]["statement_id"]
        == "claim-1"
    )
    explanation = store.explain("research", "claim-1", scopes=READ)
    assert (
        explanation["limitations"]
        and explanation["source_revision_id"] == "derived-revision:2"
    )
    assert first["generation"] == 0
    assert first["producer"]["name"] == "noesis-epistemic-engine"
    assert first["principal_id"] == "service"
    conn.close()


def test_idempotence_temporality_domain_extensions_and_output_schemas():
    conn = duckdb.connect(":memory:")
    store = EpistemicStore(conn, now=lambda: 100)
    definitions = dict(
        json.loads(
            conn.execute(
                "SELECT definitions_json FROM epistemic_taxonomies LIMIT 1"
            ).fetchone()[0]
        )
    )
    definitions["technical.reproducibility_claim"] = "A reproducibility claim."
    taxonomy = store.register_taxonomy(
        "technical", "1.0.0", definitions, domain="technical", scopes=WRITE
    )
    Draft202012Validator(
        json.loads((SCHEMAS / "noesis-epistemic-taxonomy-v1.json").read_text())
    ).validate(taxonomy)
    kwargs = {
        "principal_id": "engine",
        "scopes": WRITE,
        "source_revision_id": "derived-revision:technical:1",
        "generation": 7,
        "valid_from_ms": 10,
        "valid_to_ms": 20,
        "observed_at_ms": 30,
        "producer": {"name": "fixture", "version": "1", "revision": "abc"},
        "policy": {"domain": "technical"},
    }
    first = store.assess("technical", "claim", "The benchmark passed.", [], **kwargs)
    repeated = store.assess("technical", "claim", "The benchmark passed.", [], **kwargs)
    assert repeated["idempotent"] is True and repeated["revision"] == 1
    Draft202012Validator(
        json.loads((SCHEMAS / "noesis-epistemic-assessment-v1.json").read_text())
    ).validate(first)
    explanation = store.explain("technical", "claim", scopes=READ)
    Draft202012Validator(
        json.loads((SCHEMAS / "noesis-epistemic-explanation-v1.json").read_text())
    ).validate(explanation)
    with pytest.raises(EpistemicError, match="valid-time"):
        store.assess(
            "technical",
            "bad-time",
            "Text",
            [],
            principal_id="engine",
            scopes=WRITE,
            valid_from_ms=20,
            valid_to_ms=10,
        )
    conn.close()


def test_review_conflict_reversal_idempotence_and_audit_visibility():
    conn = duckdb.connect(":memory:")
    store = EpistemicStore(conn, now=lambda: 100)
    assessed = store.assess(
        "political",
        "claim",
        "The official allegedly intervened.",
        [],
        principal_id="engine",
        scopes=WRITE,
    )
    first = store.override(
        "political",
        "claim",
        "report",
        "The statement is explicitly attributed.",
        reviewer_id="reviewer",
        scopes=REVIEW,
        expected_assessment_id=assessed["assessment_id"],
    )
    repeated = store.override(
        "political",
        "claim",
        "report",
        "The statement is explicitly attributed.",
        reviewer_id="reviewer",
        scopes=REVIEW,
    )
    assert repeated["idempotent"] is True
    reversed_value = store.override(
        "political",
        "claim",
        "allegation",
        "Review restored the original interpretation.",
        reviewer_id="second-reviewer",
        scopes=REVIEW,
    )
    assert reversed_value["machine_status"] == "allegation"
    assert [item["sequence"] for item in reversed_value["transitions"]] == [1, 2]
    assert (
        reversed_value["transitions"][1]["predecessor_override_id"]
        == first["override"]["override_id"]
    )
    with pytest.raises(EpistemicError, match="changed after"):
        store.override(
            "political",
            "claim",
            "fact",
            "This review used a stale assessment revision.",
            reviewer_id="third-reviewer",
            scopes=REVIEW,
            expected_assessment_id="epistemic-assessment:stale",
        )
    conn.close()


@pytest.mark.parametrize(
    ("namespace", "text", "status"),
    [
        ("research", "We propose a mechanism.", "hypothesis"),
        ("political", "The minister allegedly acted.", "allegation"),
        ("economic", "Inflation is forecast to fall.", "forecast"),
        ("osint", "According to the imagery report, it moved.", "report"),
        ("technical", "The service should retry.", "normative"),
        ("scientific", "Approximately 12 percent reacted.", "estimate"),
    ],
)
def test_six_domain_classification_fixtures(namespace, text, status):
    assert namespace and classify_statement(text)["status"] == status


def test_authorization_taxonomy_immutability_and_invalid_filters():
    conn = duckdb.connect(":memory:")
    store = EpistemicStore(conn)
    with pytest.raises(EpistemicError, match="missing required scope"):
        store.assess("n", "c", "Text", [], principal_id="x", scopes=set())
    definitions = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT key,value FROM json_each((SELECT definitions_json FROM epistemic_taxonomies LIMIT 1))"
        ).fetchall()
    }
    definitions["fact"] = "changed"
    with pytest.raises(EpistemicError, match="different content"):
        store.register_taxonomy("core", "1.0.0", definitions, scopes=WRITE)
    with pytest.raises(EpistemicError, match="unsupported"):
        store.search("n", scopes=READ, statuses=["truth"])
    conn.close()
