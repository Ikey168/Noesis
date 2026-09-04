from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.freshness import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    EvidenceFreshnessStore,
    FreshnessError,
)

READ = {READ_SCOPE}
WRITE = {WRITE_SCOPE}
REVIEW = {REVIEW_SCOPE}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _policy(store, domain="economic", version="1", **rules):
    return store.register_policy(
        "research",
        domain,
        "report",
        "claim",
        version,
        {"max_age_ms": 100, "warning_before_ms": 10, **rules},
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=int(version) * 10,
        provenance={"citation": f"policy:{version}"},
        policy_context={"owner": "research-methods"},
    )


def _annotate(store, evidence_id, domain="economic", published_at_ms=100, **values):
    return store.annotate(
        "research",
        evidence_id,
        domain,
        "report",
        "claim",
        retrieved_at_ms=values.pop("retrieved_at_ms", 100),
        published_at_ms=published_at_ms,
        principal_id="analyst",
        scopes=WRITE,
        provenance={"citation": f"source:{evidence_id}"},
        **values,
    )


def test_versioned_policies_missing_dates_timeless_upgrade_and_review_override():
    conn = duckdb.connect(":memory:")
    store = EvidenceFreshnessStore(conn, now=lambda: 1_000)
    first = _policy(store, missing_date="unknown")
    assert _policy(store, missing_date="unknown")["idempotent"]
    missing = _annotate(store, "missing", published_at_ms=None)
    assert missing["evidence_id"] == "missing"
    assert (
        store.assess("research", "missing", at_ms=200, scopes=READ, persist=False)[
            "state"
        ]
        == "unknown"
    )

    timeless = store.register_policy(
        "research",
        "scientific",
        "report",
        "claim",
        "1",
        {"max_age_ms": None, "missing_date": "timeless"},
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=10,
    )
    _annotate(store, "constant", domain="scientific", published_at_ms=None)
    assert (
        store.assess("research", "constant", at_ms=99_999, scopes=READ, persist=False)[
            "state"
        ]
        == "timeless"
    )

    second = store.register_policy(
        "research",
        "economic",
        "report",
        "claim",
        "2",
        {"max_age_ms": 1_000},
        supersedes_policy_id=first["policy_id"],
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=20,
    )
    assert (
        store.policy(first["policy_id"], namespace="research", scopes=READ)["status"]
        == "superseded"
    )
    assert (
        store.select_policy("research", "economic", "report", "claim", scopes=READ)[
            "policy_id"
        ]
        == second["policy_id"]
    )
    with pytest.raises(FreshnessError, match="different content"):
        store.register_policy(
            "research",
            "economic",
            "report",
            "claim",
            "2",
            {"max_age_ms": 5},
            supersedes_policy_id=first["policy_id"],
            principal_id="analyst",
            scopes=WRITE,
            observed_at_ms=20,
        )
    reviewed = store.override(
        "research",
        "missing",
        "fresh",
        valid_until_ms=300,
        reason="verified against the issuing registry",
        evidence=[{"citation": "review:1"}],
        principal_id="reviewer",
        scopes=REVIEW,
    )
    assert store.override(
        "research",
        "missing",
        "fresh",
        valid_until_ms=300,
        reason="verified against the issuing registry",
        evidence=[{"citation": "review:1"}],
        principal_id="reviewer",
        scopes=REVIEW,
    )["idempotent"]
    assert (
        store.assess("research", "missing", at_ms=250, scopes=READ, persist=False)[
            "override"
        ]["override_id"]
        == reviewed["override_id"]
    )
    _validate("noesis-evidence-freshness-policy-v1.json", timeless)
    conn.close()


def test_applicability_partial_jurisdiction_correction_and_conflicting_successors():
    conn = duckdb.connect(":memory:")
    store = EvidenceFreshnessStore(conn, now=lambda: 500)
    _policy(store)
    for evidence_id in ("old", "correction-a", "correction-b"):
        _annotate(store, evidence_id)
    partial = store.relate(
        "research",
        "old",
        "correction-a",
        "narrows",
        applicability={"fraction": 0.4, "jurisdiction": "DE"},
        confidence=0.9,
        evidence=[{"citation": "correction-notice:1"}],
        provenance={"publisher": "statistics-office"},
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=150,
    )
    assert (
        "partial-narrows"
        not in store.assess(
            "research",
            "old",
            at_ms=150,
            context={"jurisdiction": "FR"},
            scopes=READ,
            persist=False,
        )["reasons"]
    )
    german = store.assess(
        "research",
        "old",
        at_ms=150,
        context={"jurisdiction": "DE"},
        scopes=READ,
        persist=False,
    )
    assert german["valid"] and "partial-narrows" in german["reasons"]
    for later in ("correction-a", "correction-b"):
        store.relate(
            "research",
            "old",
            later,
            "supersedes",
            applicability={"fraction": 1, "jurisdiction": "DE"},
            confidence=0.8,
            evidence=[{"citation": f"notice:{later}"}],
            provenance={"kind": "official-correction"},
            principal_id="analyst",
            scopes=WRITE,
            observed_at_ms=160,
        )
    corrected = store.assess(
        "research",
        "old",
        at_ms=170,
        context={"jurisdiction": "DE"},
        scopes=READ,
        persist=False,
    )
    assert corrected["state"] == "expired"
    assert "conflicting-successors" in corrected["reasons"]
    _validate("noesis-evidence-applicability-relation-v1.json", partial)
    conn.close()


def test_explainable_assessment_boundaries_health_methodology_event_and_replay():
    conn = duckdb.connect(":memory:")
    store = EvidenceFreshnessStore(conn, now=lambda: 1_000)
    _policy(
        store,
        source_health_required=True,
        cadence_ms=150,
        event_close_grace_ms=20,
        methodology_revision="2026",
        methodology_change="invalidate",
        decay_half_life_ms=100,
    )
    _annotate(store, "boundary", methodology_revision="2026")
    boundary = store.assess(
        "research", "boundary", at_ms=200, principal_id="analyst", scopes=WRITE
    )
    assert boundary["state"] == "expiring-soon"
    assert boundary["valid"]  # stale/expiring evidence is retained rather than deleted.
    assert boundary["decay_score"] == 0.5
    assert store.replay("research", boundary["assessment_id"], scopes=READ)[
        "deterministic"
    ]
    assert (
        store.assessment("research", boundary["assessment_id"], scopes=READ)["reasons"]
        == boundary["reasons"]
    )
    _validate("noesis-evidence-freshness-assessment-v1.json", boundary)

    _annotate(store, "future", published_at_ms=300, methodology_revision="2026")
    assert (
        store.assess("research", "future", at_ms=200, scopes=READ, persist=False)[
            "state"
        ]
        == "invalid"
    )
    _annotate(
        store,
        "closed",
        event_closed_at_ms=150,
        methodology_revision="2026",
    )
    assert (
        "event-closed"
        in store.assess("research", "closed", at_ms=170, scopes=READ, persist=False)[
            "reasons"
        ]
    )
    _annotate(
        store,
        "unhealthy",
        methodology_revision="old",
        source_health={"status": "unhealthy"},
        retrieved_at_ms=0,
    )
    unhealthy = store.assess(
        "research", "unhealthy", at_ms=200, scopes=READ, persist=False
    )
    assert unhealthy["state"] == "invalid"
    assert {"source-cadence-overdue", "source-unhealthy", "methodology-changed"} <= set(
        unhealthy["reasons"]
    )
    conn.close()


def test_propagation_last_support_mixed_age_dedup_and_recovery():
    conn = duckdb.connect(":memory:")
    clock = iter(range(1_000, 1_100))
    store = EvidenceFreshnessStore(conn, now=lambda: next(clock))
    _policy(store)
    _annotate(store, "stale", published_at_ms=0)
    _annotate(store, "current", published_at_ms=150)
    for kind in ("claim", "answer", "brief", "watch", "search", "assessment"):
        store.dependency(
            "research",
            "stale",
            kind,
            f"{kind}:1",
            {},
            principal_id="analyst",
            scopes=WRITE,
        )
    store.dependency(
        "research",
        "current",
        "answer",
        "answer:1",
        {},
        principal_id="analyst",
        scopes=WRITE,
    )
    first = store.propagate("research", at_ms=200, principal_id="analyst", scopes=WRITE)
    by_kind = {item["consumer_kind"]: item for item in first["items"]}
    assert by_kind["answer"]["state"] == "mixed-age"
    assert by_kind["search"]["state"] == "unsupported-currently"
    assert by_kind["search"]["reason"] == "last-current-support-lost"
    repeated = store.propagate(
        "research", at_ms=200, principal_id="analyst", scopes=WRITE
    )
    assert all(item["idempotent"] for item in repeated["items"])
    assert (
        conn.execute("SELECT count(*) FROM evidence_freshness_impacts").fetchone()[0]
        == 6
    )
    store.override(
        "research",
        "stale",
        "fresh",
        valid_until_ms=300,
        reason="source refreshed out of band",
        evidence=[{"citation": "review:refresh"}],
        principal_id="reviewer",
        scopes=REVIEW,
    )
    recovered = store.propagate(
        "research", at_ms=200, principal_id="analyst", scopes=WRITE
    )
    assert {item["reason"] for item in recovered["items"]} == {"freshness-recovered"}
    _validate("noesis-evidence-freshness-impact-v1.json", recovered["items"][0])
    conn.close()


def test_simulation_is_side_effect_free_bounded_authorized_and_six_domain_policy_compare():
    conn = duckdb.connect(":memory:")
    store = EvidenceFreshnessStore(conn, now=lambda: 1_000)
    domains = ("research", "political", "economic", "osint", "technical", "scientific")
    evidence_ids = []
    for domain in domains:
        old = _policy(store, domain=domain, version="1")
        evidence_id = f"evidence:{domain}"
        evidence_ids.append(evidence_id)
        _annotate(store, evidence_id, domain=domain)
        store.register_policy(
            "research",
            domain,
            "report",
            "claim",
            "2",
            {"max_age_ms": 1_000},
            supersedes_policy_id=old["policy_id"],
            principal_id="analyst",
            scopes=WRITE,
            observed_at_ms=20,
        )
    before = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("evidence_freshness_assessments", "evidence_freshness_audit")
    }
    simulated = store.simulate(
        "research", evidence_ids, at_ms=250, limit=3, scopes=READ
    )
    assert simulated["scanned"] == 3
    assert simulated == store.simulate(
        "research", evidence_ids, at_ms=250, limit=3, scopes=READ
    )
    assert (
        store.simulate(
            "research", evidence_ids, at_ms=250, cancel_requested=True, scopes=READ
        )["status"]
        == "cancelled"
    )
    comparison = store.compare_policies(
        "research",
        [evidence_ids[0]],
        at_ms=250,
        old_version="1",
        new_version="2",
        scopes=READ,
    )
    assert comparison["transitions"][0]["old_state"] == "stale"
    assert comparison["transitions"][0]["new_state"] == "fresh"
    after = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
    with pytest.raises(FreshnessError, match="missing required scope"):
        store.simulate("research", evidence_ids, at_ms=250, scopes={"knowledge:read"})
    assert (
        store.expiring("research", at_ms=950, horizon_ms=100, limit=2, scopes=READ)[
            "scanned"
        ]
        == 2
    )
    conn.close()
