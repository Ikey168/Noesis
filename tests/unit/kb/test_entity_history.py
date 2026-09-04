from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.entity_history import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    EntityHistoryError,
    EntityHistoryStore,
)

S = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def val(n, v):
    Draft202012Validator(json.loads((S / n).read_text())).validate(v)


def entities(s, namespace="research"):
    for e in ("entity:a", "entity:b", "entity:c"):
        s.register_entity(namespace, e, [e[-1]], principal_id="c", scopes={WRITE_SCOPE})


def test_decision_duplicates_reviewer_conflict_reversal_stable_event():
    c = duckdb.connect(":memory:")
    s = EntityHistoryStore(c, now=lambda: 100)
    entities(s)
    a = s.decide(
        "research",
        "match",
        ["entity:a", "entity:b"],
        {"evidence": ["e1"]},
        event_key="case:1",
        reviewer_id="r1",
        principal_id="r1",
        scopes={REVIEW_SCOPE},
    )
    assert s.decide(
        "research",
        "match",
        ["entity:a", "entity:b"],
        {"evidence": ["e1"]},
        event_key="case:1",
        reviewer_id="r1",
        principal_id="r1",
        scopes={REVIEW_SCOPE},
    )["idempotent"]
    b = s.decide(
        "research",
        "non-match",
        ["entity:a", "entity:b"],
        {"evidence": ["e2"]},
        event_key="case:1",
        reviewer_id="r2",
        principal_id="r2",
        scopes={REVIEW_SCOPE},
    )
    assert (
        b["reviewer_conflict"]
        and b["event_key"] == a["event_key"]
        and b["revision"] == 2
    )
    val("noesis-entity-identity-decision-v1.json", b)
    c.close()


def test_merge_chains_concurrent_dual_control_cycles_namespace_undo():
    c = duckdb.connect(":memory:")
    s = EntityHistoryStore(c, now=lambda: 100)
    entities(s)
    p = s.merge_preview(
        "research",
        ["entity:a"],
        "entity:b",
        scopes={READ_SCOPE},
        dual_control=True,
        approvals=["r1"],
    )
    assert not p["eligible"]
    p = s.merge_preview(
        "research",
        ["entity:a"],
        "entity:b",
        scopes={READ_SCOPE},
        dual_control=True,
        approvals=["r1", "r2"],
    )
    m = s.execute_merge(
        "research", p, reviewer_id="r2", principal_id="r2", scopes={EXECUTE_SCOPE}
    )
    assert (
        s.resolve("research", "entity:a", scopes={READ_SCOPE})["canonical_id"]
        == "entity:b"
    )
    chain = s.execute_merge(
        "research",
        s.merge_preview("research", ["entity:b"], "entity:c", scopes={READ_SCOPE}),
        reviewer_id="r",
        principal_id="r",
        scopes={EXECUTE_SCOPE},
    )
    assert (
        s.resolve("research", "entity:a", scopes={READ_SCOPE})["canonical_id"]
        == "entity:c"
    )
    with pytest.raises(EntityHistoryError, match="cycle"):
        s.merge_preview("research", ["entity:c"], "entity:a", scopes={READ_SCOPE})
    assert s.undo(
        "research",
        chain["decision_id"],
        reviewer_id="r",
        principal_id="r",
        scopes={EXECUTE_SCOPE},
    )["reversible"]
    assert (
        s.resolve("research", "entity:a", scopes={READ_SCOPE})["canonical_id"]
        == "entity:b"
    )
    with pytest.raises(EntityHistoryError, match="not found"):
        s.merge_preview("other", ["entity:a"], "entity:b", scopes={READ_SCOPE})
        val("noesis-entity-merge-v1.json", m)
        c.close()


def test_split_partial_ambiguous_aliases_reassignment_and_rollback():
    c = duckdb.connect(":memory:")
    s = EntityHistoryStore(c, now=lambda: 100)
    entities(s)
    p = s.split_preview(
        "research",
        "entity:a",
        [
            {"entity_id": "entity:a1", "aliases": ["A one"]},
            {"entity_id": "entity:a2", "aliases": ["A two"]},
        ],
        [{"object_type": "mention", "object_id": "m1", "entity_id": "entity:a1"}],
        ambiguous_object_ids=["m1", "m2"],
        scopes={READ_SCOPE},
    )
    assert p["partial"] and p["ambiguous_object_ids"] == ["m2"]
    x = s.execute_split(
        "research", p, reviewer_id="r", principal_id="r", scopes={EXECUTE_SCOPE}
    )
    row = c.execute(
        "SELECT entity_id FROM entity_history_assignments WHERE object_id='m1' AND active=true"
    ).fetchone()
    assert row == ("entity:a1",)
    s.undo(
        "research",
        x["decision_id"],
        reviewer_id="r",
        principal_id="r",
        scopes={EXECUTE_SCOPE},
    )
    assert not c.execute(
        "SELECT active FROM entity_history_assignments WHERE object_id='m1'"
    ).fetchone()[0]
    val("noesis-entity-split-v1.json", x)
    c.close()


def test_impact_independent_snapshot_failed_rebuild_atomic_publication():
    c = duckdb.connect(":memory:")
    s = EntityHistoryStore(c, now=lambda: 100)
    entities(s)
    s.add_dependency(
        "research", "entity:a", "graph", "g1", principal_id="w", scopes={WRITE_SCOPE}
    )
    s.add_dependency(
        "research",
        "entity:a",
        "bundle",
        "b1",
        independent=True,
        principal_id="w",
        scopes={WRITE_SCOPE},
    )
    impact = s.impact("research", ["entity:a"], scopes={READ_SCOPE})
    assert (
        impact["rebuild_types"] == ["graph"]
        and impact["independent"][0]["dependent_id"] == "b1"
    )
    failed = s.publish_rebuild(
        "research",
        "d",
        1,
        [{"type": "graph", "status": "failed"}],
        principal_id="w",
        scopes={EXECUTE_SCOPE},
    )
    assert not failed["published"] and c.execute(
        "SELECT count(*) FROM entity_history_publications"
    ).fetchone() == (0,)
    good = s.publish_rebuild(
        "research",
        "d",
        1,
        [{"type": "graph", "status": "completed"}],
        principal_id="w",
        scopes={EXECUTE_SCOPE},
    )
    assert good["published"]
    val("noesis-entity-impact-v1.json", impact)
    val("noesis-entity-impact-v1.json", good)
    c.close()


def test_authorization_audit_export_six_domains():
    c = duckdb.connect(":memory:")
    s = EntityHistoryStore(c, now=lambda: 100)
    for ns in ("research", "political", "economic", "osint", "technical", "scientific"):
        entities(s, ns)
        s.decide(
            ns,
            "review",
            ["entity:a"],
            {"accepted": True},
            reviewer_id="r",
            principal_id="r",
            scopes={REVIEW_SCOPE},
        )
        e = s.export(ns, ["entity:a"], scopes={READ_SCOPE})
        assert e["audit_complete"]
        val("noesis-entity-history-export-v1.json", e)
    with pytest.raises(EntityHistoryError, match="missing required scope"):
        s.history("research", "entity:a", scopes={"knowledge:read"})
        c.close()
