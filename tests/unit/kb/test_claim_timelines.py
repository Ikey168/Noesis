from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.claim_timelines import ClaimTimelineError, ClaimTimelineStore

READ = {"knowledge:claim-timeline:read"}
WRITE = {"knowledge:claim-timeline:write"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _add_claim(conn, claim_id, text, document=None):
    conn.execute(
        "INSERT INTO argument_claims(claim_id,claim_text,document_id,source_type) VALUES (?,?,?,'news')",
        [claim_id, text, document or f"doc:{claim_id}"],
    )


def _state(store, claim_id, **updates):
    values = {
        "source_id": f"source:{claim_id}",
        "source_revision_id": f"document-revision:{claim_id}:1",
        "evidence": [{"citation": f"citation:{claim_id}"}],
        "principal_id": "analyst",
        "scopes": WRITE,
        "observed_at_ms": 100,
    }
    values.update(updates)
    return store.capture_state("economic", claim_id, **values)


def test_stable_claim_identity_many_to_many_lineage_cycles_and_retracted_sources():
    conn = duckdb.connect(":memory:")
    store = ClaimTimelineStore(conn, now=lambda: 100)
    for claim_id, text in (
        ("c1", "Output grew."),
        ("c2", "Production increased."),
        ("c3", "GDP rose."),
    ):
        _add_claim(conn, claim_id, text)
        _state(store, claim_id)
    one = store.link(
        "economic",
        "c1",
        "c3",
        "successor",
        confidence=0.8,
        evidence=[{"citation": "report:1", "retracted": True}],
        explanation={"reason": "same statistic"},
        method={"kind": "review"},
        principal_id="analyst",
        scopes=WRITE,
    )
    two = store.link(
        "economic",
        "c2",
        "c3",
        "refinement",
        confidence=0.7,
        evidence=[{"citation": "report:2"}],
        explanation={"reason": "more precise"},
        method={"kind": "review"},
        principal_id="analyst",
        scopes=WRITE,
    )
    assert one["evidence_status"] == "retracted-only"
    assert store.link(
        "economic",
        "c1",
        "c3",
        "successor",
        confidence=0.8,
        evidence=[{"citation": "report:1", "retracted": True}],
        explanation={"reason": "same statistic"},
        method={"kind": "review"},
        principal_id="analyst",
        scopes=WRITE,
    )["idempotent"]
    with pytest.raises(ClaimTimelineError, match="cycle"):
        store.link(
            "economic",
            "c3",
            "c1",
            "reversal",
            confidence=0.5,
            evidence=[{"citation": "report:3"}],
            explanation={},
            method={"kind": "review"},
            principal_id="analyst",
            scopes=WRITE,
        )
    timeline = store.timeline("economic", "c3", scopes=READ)
    assert {edge["edge_id"] for edge in timeline["edges"]} == {
        one["edge_id"],
        two["edge_id"],
    }
    _validate("noesis-claim-lineage-v1.json", one)
    _validate("noesis-claim-timeline-v1.json", timeline)
    conn.close()


def test_successor_detection_paraphrase_numeric_negation_scope_false_match_and_pin():
    conn = duckdb.connect(":memory:")
    store = ClaimTimelineStore(conn, now=lambda: 100)
    claims = {
        "old": "GDP grew 2 percent in Germany",
        "paraphrase": "Economic output increased 2 percent in Germany",
        "numeric": "Economic output increased 3 percent in Germany",
        "negated": "GDP did not grow 2 percent in Germany",
        "scope": "GDP grew 2 percent in Europe",
        "false": "Rain fell across the northern coast",
    }
    for claim_id, text in claims.items():
        _add_claim(conn, claim_id, text)
        _state(
            store, claim_id, scope={"geography": "DE" if claim_id != "scope" else "EU"}
        )
    result = store.match_successors(
        "economic",
        "old",
        candidate_claim_ids=list(claims)[1:],
        threshold=0.4,
        limit=10,
        principal_id="analyst",
        scopes=READ,
    )
    relations = {
        item["candidate_claim_id"]: item["relation"] for item in result["matches"]
    }
    assert relations["paraphrase"] == "successor"
    assert relations["numeric"] == "refinement"
    assert relations["negated"] == "reversal"
    assert relations["scope"] == "refinement"
    assert "false" not in relations
    with pytest.raises(ClaimTimelineError, match="require name"):
        store.match_successors(
            "economic",
            "old",
            candidate_claim_ids=["false"],
            embedding_scores={"false": 0.9},
            principal_id="analyst",
            scopes=READ,
        )
    modeled = store.match_successors(
        "economic",
        "old",
        candidate_claim_ids=["false"],
        embedding_scores={"false": 0.9},
        embedding_pin={"name": "claims", "version": "2", "revision": "abc"},
        principal_id="analyst",
        scopes=READ,
    )
    assert modeled["matches"][0]["explanation"]["embedding_score"] == 0.9
    _validate("noesis-claim-successor-match-v1.json", result)
    conn.close()


def test_semantic_diff_hedging_quotes_units_and_conflicting_interpretations():
    conn = duckdb.connect(":memory:")
    store = ClaimTimelineStore(conn, now=lambda: 100)
    _add_claim(conn, "early", "The minister said the route is 1 km.")
    _add_claim(conn, "late", "The ministry said the route may be 1000 m.")
    early = _state(
        store,
        "early",
        stance="supports",
        certainty=0.9,
        epistemic_status="reported",
        attribution={"speaker": "minister", "quote": "is 1 km"},
        quantities=[{"role": "route", "value": "1", "unit": "km"}],
        interpretations=[{"reading": "exact"}],
    )
    late = _state(
        store,
        "late",
        stance="mixed",
        certainty=0.5,
        epistemic_status="uncertain",
        attribution={"speaker": "ministry", "quote": "may be 1000 m"},
        quantities=[{"role": "route", "value": "1000", "unit": "m"}],
        interpretations=[{"reading": "estimate"}, {"reading": "upper-bound"}],
    )
    diff = store.diff("economic", "early", "late", scopes=READ)
    assert {
        "wording",
        "stance",
        "certainty",
        "epistemic_status",
        "attribution",
        "quantities",
        "interpretations",
    } <= diff["changes"].keys()
    assert diff["changes"]["quantities"]["equivalent_after_conversion"]
    assert diff["citation_closure"]["left"] == early["evidence"]
    _validate("noesis-claim-state-v1.json", late)
    _validate("noesis-claim-semantic-diff-v1.json", diff)
    conn.close()


def test_as_of_branching_timeline_pagination_missing_revision_and_ordering():
    conn = duckdb.connect(":memory:")
    store = ClaimTimelineStore(conn, now=lambda: 100)
    for index in range(4):
        claim_id = f"c{index}"
        _add_claim(conn, claim_id, f"Claim version {index}")
        _state(store, claim_id, observed_at_ms=100 + index * 10, generation=index)
    for target, relation in (
        ("c1", "successor"),
        ("c2", "branch"),
        ("c3", "withdrawal"),
    ):
        store.link(
            "economic",
            "c0",
            target,
            relation,
            confidence=0.8,
            evidence=[{"citation": f"edge:{target}"}],
            explanation={},
            method={"kind": "review"},
            observed_at_ms=100 + int(target[1:]) * 10,
            generation=int(target[1:]),
            principal_id="analyst",
            scopes=WRITE,
        )
    first = store.timeline("economic", "c0", scopes=READ, limit=2)
    second = store.timeline(
        "economic", "c0", scopes=READ, limit=2, cursor=first["next_cursor"]
    )
    assert [item["claim_id"] for item in first["items"] + second["items"]] == [
        "c0",
        "c1",
        "c2",
        "c3",
    ]
    pinned = store.timeline("economic", "c0", scopes=READ, as_of_ms=115)
    assert [item["claim_id"] for item in pinned["items"]] == ["c0", "c1"]
    with pytest.raises(ClaimTimelineError, match="missing"):
        store.diff("economic", "c0", "c1", scopes=READ, left_revision=99)
    with pytest.raises(ClaimTimelineError, match="different timeline"):
        store.timeline(
            "economic", "c0", scopes=READ, generation=1, cursor=first["next_cursor"]
        )
    conn.close()


def test_source_comparison_replay_six_domains_and_auth():
    conn = duckdb.connect(":memory:")
    store = ClaimTimelineStore(conn, now=lambda: 100)
    domains = ["research", "political", "economic", "osint", "technical", "scientific"]
    for index, domain in enumerate(domains):
        claim_id = f"{domain}:claim"
        _add_claim(conn, claim_id, f"{domain} source reported value {index}")
        store.capture_state(
            domain,
            claim_id,
            source_id=f"source:{index % 2}",
            source_revision_id=f"revision:{index}",
            evidence=[{"citation": f"citation:{domain}"}],
            principal_id="analyst",
            scopes=WRITE,
            observed_at_ms=100 + index,
        )
    comparison = store.compare_sources(
        "economic", ["source:0", "source:1"], scopes=READ
    )
    assert comparison["states"]
    replay = store.replay("economic", "economic:claim", scopes=READ)
    assert replay["deterministic"] and replay["citation_closed"]
    with pytest.raises(ClaimTimelineError, match="required scope"):
        store.timeline("economic", "economic:claim", scopes=set())
    conn.close()
