from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.knowledge_quality import (
    CALCULATE_SCOPE,
    DIMENSIONS,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    QualityError,
    QualityStore,
)

S = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def val(n, v):
    Draft202012Validator(json.loads((S / n).read_text())).validate(v)


def policy(s, namespace="scientific", version="1", **kw):
    d = {x: {"weight": 1, "default": None} for x in DIMENSIONS}
    v = {
        "namespace": namespace,
        "policy_id": "quality",
        "version": version,
        "dimensions": d,
        "domain_overrides": {"political": {"freshness": 0.4}},
        "calibration": {"dataset": "human-eval-v1"},
        "principal_id": "c",
        "scopes": {WRITE_SCOPE},
    }
    v.update(kw)
    return s.register_policy(**v)


def assess(s, p, obj="claim:1", features=None, generation=1, **kw):
    v = {
        "namespace": p["namespace"],
        "object_type": "claim",
        "object_id": obj,
        "generation": generation,
        "policy_revision_id": p["policy_revision_id"],
        "features": features
        or {
            "coverage": 0.8,
            "provenance": 0.9,
            "independence": 0.7,
            "freshness": 0.6,
            "contradiction": 0.8,
            "methodology": 0.7,
            "reproducibility": 0.5,
            "uncertainty": 0.6,
        },
        "input_lineage": [{"evidence_id": "e1", "independence_group": "g1"}],
        "principal_id": "w",
        "scopes": {CALCULATE_SCOPE},
    }
    v.update(kw)
    return s.assess(**v)


def test_policy_domain_defaults_missing_dimensions_versions():
    c = duckdb.connect(":memory:")
    s = QualityStore(c, now=lambda: 100)
    p = policy(s)
    a = assess(s, p, features={"coverage": 0.5}, domain="political")
    assert (
        a["dimensions"]["freshness"]["value"] == 0.4
        and "methodology" in a["missing_dimensions"]
    )
    assert a["transparent_defaults"][0]["source"] == "domain-override"
    p2 = policy(s, version="2", threshold=0.7)
    assert p2["policy_revision_id"] != p["policy_revision_id"]
    val("noesis-quality-policy-v1.json", p2)
    val("noesis-quality-assessment-v1.json", a)
    c.close()


def test_sparse_inaccessible_retracted_incremental_replay_and_types():
    c = duckdb.connect(":memory:")
    s = QualityStore(c, now=lambda: 100)
    p = policy(s)
    a = assess(
        s,
        p,
        features={"coverage": 0.2, "inaccessible_sources": True, "retracted": True},
    )
    assert {"inaccessible-sources", "retracted-input"} <= set(a["flags"])
    assert s.replay("scientific", a["assessment_id"], scopes={READ_SCOPE})[
        "deterministic"
    ]
    new = assess(s, p, generation=2, features={"coverage": 0.9})
    assert new["assessment_id"] != a["assessment_id"]
    for t in ("source", "document", "entity", "event", "dataset", "answer", "bundle"):
        assert (
            s.assess(
                "scientific",
                t,
                t,
                1,
                p["policy_revision_id"],
                {"coverage": 0.5},
                input_lineage=[],
                principal_id="w",
                scopes={CALCULATE_SCOPE},
            )["object_type"]
            == t
        )
    c.close()


def test_correlated_collection_small_sample_drift_policy_simulation():
    c = duckdb.connect(":memory:")
    s = QualityStore(c, now=lambda: 100)
    p = policy(s)
    a = assess(s, p)
    b = assess(
        s,
        p,
        obj="claim:2",
        input_lineage=[{"evidence_id": "e2", "independence_group": "g1"}],
    )
    collection = s.collection(
        "scientific",
        [a["assessment_id"], b["assessment_id"]],
        scopes={CALCULATE_SCOPE},
        calibration_samples=[0.9],
        reference_distribution={"mean": 0.1},
    )
    assert collection["independent_groups"] == 1 and {
        "small-calibration-sample",
        "calibration-distribution-drift",
    } <= set(collection["warnings"])
    p2 = policy(
        s,
        version="2",
        dimensions={x: {"weight": 1 if x == "coverage" else 0} for x in DIMENSIONS},
    )
    sim = s.simulate(
        "scientific",
        [a["assessment_id"]],
        p2["policy_revision_id"],
        scopes={READ_SCOPE},
    )
    assert sim["side_effect_free"] and sim["items"][0]["after"] == 0.8
    val("noesis-quality-collection-v1.json", collection)
    c.close()


def test_ranking_threshold_degraded_overrides_never_erase():
    c = duckdb.connect(":memory:")
    s = QualityStore(c, now=lambda: 100)
    p = policy(s)
    low = assess(s, p, features={x: 0.2 for x in DIMENSIONS})
    high = assess(s, p, obj="claim:2", features={x: 0.8 for x in DIMENSIONS})
    rank = s.rank(
        "scientific",
        [low["assessment_id"], high["assessment_id"]],
        scopes={READ_SCOPE},
        threshold=0.5,
    )
    assert (
        rank["items"][0]["object_id"] == "claim:2"
        and rank["low_scores_retained"]
        and len(rank["items"]) == 2
    )
    over = s.rank(
        "scientific",
        [low["assessment_id"], high["assessment_id"]],
        scopes={READ_SCOPE},
        user_overrides={"claim:1": 0.95},
    )
    assert over["items"][0]["override"]
    health = s.health("scientific", [low["assessment_id"]], scopes={READ_SCOPE})
    assert not health["degraded"]
    val("noesis-quality-ranking-v1.json", rank)
    val("noesis-quality-health-v1.json", health)
    c.close()


def test_auth_bounded_reproducibility_six_domain_calibration_review():
    c = duckdb.connect(":memory:")
    s = QualityStore(c, now=lambda: 100)
    for ns in ("research", "political", "economic", "osint", "technical", "scientific"):
        p = policy(s, namespace=ns)
        a = assess(s, p)
        assert s.get(ns, a["assessment_id"], scopes={READ_SCOPE})["input_lineage"]
        assert (
            s.override(
                ns,
                a["object_id"],
                "coverage",
                0.75,
                "human calibration",
                reviewer_id="human",
                principal_id="human",
                scopes={REVIEW_SCOPE},
            )["value"]
            == 0.75
        )
    with pytest.raises(QualityError, match="missing required scope"):
        s.health("scientific", [], scopes={"knowledge:read"})
    c.close()
