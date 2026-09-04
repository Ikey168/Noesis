from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.change_briefs import (
    DELIVER_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ChangeBriefError,
    ChangeBriefStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _v(n, x):
    Draft202012Validator(json.loads((SCHEMAS / n).read_text())).validate(x)


def _policy(s, namespace="economic", **kw):
    v = {
        "namespace": namespace,
        "policy_id": "materiality",
        "version": "1",
        "principal_id": "curator",
        "scopes": {WRITE_SCOPE},
        "generation": 2,
        "observed_at_ms": 10,
        "producer": {"id": "policy"},
        "policy_context": {"audience": "analyst"},
        "provenance": {"source": "config"},
    }
    v.update(kw)
    return s.register_policy(**v)


def _preview(s, kind="metric", before=100, after=110, **kw):
    v = {
        "namespace": "economic",
        "object_type": kind,
        "object_id": "gdp",
        "before": before,
        "after": after,
        "from_generation": 1,
        "to_generation": 2,
        "evidence_before": [{"citation_id": "old"}],
        "evidence_after": [{"citation_id": "new"}],
        "scopes": {READ_SCOPE},
    }
    v.update(kw)
    return s.preview(**v)


def test_add_remove_correct_reclassify_stable_identity():
    c = duckdb.connect(":memory:")
    s = ChangeBriefStore(c, now=lambda: 100)
    p = _policy(s)
    assert _preview(s, before=None)["classification"] == "addition"
    assert _preview(s, after=None)["classification"] == "removal"
    correction = _preview(s, before="old", after="new")
    assert correction["classification"] == "correction"
    reclass = _preview(
        s,
        kind="claim",
        before={"classification": "fact"},
        after={"classification": "opinion"},
    )
    assert reclass["classification"] == "reclassification"
    one = s.generate(
        "economic",
        p["policy_revision_id"],
        correction,
        principal_id="worker",
        scopes={WRITE_SCOPE},
    )
    two = s.generate(
        "economic",
        p["policy_revision_id"],
        correction,
        principal_id="worker",
        scopes={WRITE_SCOPE},
    )
    assert one["brief_id"] == two["brief_id"] and two["idempotent"]
    _v("noesis-semantic-change-event-v1.json", correction)
    _v("noesis-change-brief-v1.json", one)
    c.close()


def test_cosmetic_numeric_retraction_ties_deterministic_ranking():
    c = duckdb.connect(":memory:")
    s = ChangeBriefStore(c, now=lambda: 100)
    p = _policy(s)
    cosmetic = _preview(s, before="Hello, world!", after="hello world")
    assert cosmetic["classification"] == "cosmetic"
    assert not s.generate(
        "economic",
        p["policy_revision_id"],
        cosmetic,
        principal_id="w",
        scopes={WRITE_SCOPE},
    )["material"]
    numeric = _preview(s)
    assert numeric["classification"] == "numeric-change"
    retract = _preview(
        s, kind="claim", before={"status": "active"}, after={"status": "retracted"}
    )
    assert retract["classification"] == "retraction"
    n = s.generate(
        "economic",
        p["policy_revision_id"],
        numeric,
        principal_id="w",
        scopes={WRITE_SCOPE},
    )
    r = s.generate(
        "economic",
        p["policy_revision_id"],
        retract,
        principal_id="w",
        scopes={WRITE_SCOPE},
    )
    first_order = [
        item["brief_id"] for item in s.history("economic", scopes={READ_SCOPE})["items"]
    ]
    second_order = [
        item["brief_id"] for item in s.history("economic", scopes={READ_SCOPE})["items"]
    ]
    assert n["score"] == r["score"] and first_order == second_order
    c.close()


def test_conflicts_missing_prior_partial_coverage_no_unsupported_synthesis():
    c = duckdb.connect(":memory:")
    s = ChangeBriefStore(c, now=lambda: 100)
    p = _policy(s)
    preview = _preview(
        s,
        before=None,
        coverage_before=False,
        evidence_before=[],
        evidence_after=[
            {"citation_id": "a", "stance": "supports"},
            {"citation_id": "b", "stance": "contradicts"},
        ],
    )
    brief = s.generate(
        "economic",
        p["policy_revision_id"],
        preview,
        principal_id="w",
        scopes={WRITE_SCOPE},
    )
    assert {"coverage-changed-or-incomplete", "missing-prior-evidence"} <= set(
        brief["uncertainty"]
    )
    assert brief["after"]["evidence"] == preview["evidence_after"]
    assert s.replay("economic", brief["brief_id"], scopes={READ_SCOPE})["deterministic"]
    cancelled = s.generate(
        "economic",
        p["policy_revision_id"],
        preview,
        principal_id="w",
        scopes={WRITE_SCOPE},
        cancel_requested=True,
    )
    assert cancelled["status"] == "cancelled"
    c.close()


def test_burst_dedup_retry_cancel_subscriber_isolation_and_quiet():
    c = duckdb.connect(":memory:")
    s = ChangeBriefStore(c, now=lambda: 100)
    p = _policy(s)
    b = s.generate(
        "economic",
        p["policy_revision_id"],
        _preview(s),
        principal_id="w",
        scopes={WRITE_SCOPE},
    )
    a = s.subscribe(
        "economic",
        "alice",
        1000,
        {"material_only": True},
        principal_id="alice",
        scopes={WRITE_SCOPE},
    )
    bob = s.subscribe(
        "economic", "bob", 1000, {}, principal_id="bob", scopes={WRITE_SCOPE}
    )
    delivery = s.deliver(
        "economic",
        a["subscription_id"],
        0,
        200,
        principal_id="worker",
        scopes={DELIVER_SCOPE},
    )
    assert [x["brief_id"] for x in delivery["items"]] == [b["brief_id"]]
    retry = s.deliver(
        "economic",
        a["subscription_id"],
        0,
        200,
        principal_id="worker",
        scopes={DELIVER_SCOPE},
    )
    assert retry["retry"] and retry["attempts"] == 2
    assert (
        s.acknowledge(
            "economic",
            delivery["delivery_id"],
            principal_id="alice",
            scopes={DELIVER_SCOPE},
        )["status"]
        == "acknowledged"
    )
    assert s.deliver(
        "economic",
        bob["subscription_id"],
        200,
        300,
        principal_id="worker",
        scopes={DELIVER_SCOPE},
    )["quiet"]
    assert (
        s.deliver(
            "economic",
            bob["subscription_id"],
            0,
            200,
            principal_id="worker",
            scopes={DELIVER_SCOPE},
            cancel_requested=True,
        )["status"]
        == "cancelled"
    )
    _v("noesis-change-brief-delivery-v1.json", delivery)
    c.close()


def test_snapshot_history_auth_pagination_feedback_export_domains():
    c = duckdb.connect(":memory:")
    s = ChangeBriefStore(c, now=lambda: 100)
    for namespace in ("political", "economic", "technical", "scientific"):
        p = _policy(s, namespace=namespace)
        preview = s.preview(
            namespace,
            "claim",
            f"claim:{namespace}",
            "old",
            "new",
            1,
            2,
            evidence_before=[{"c": "1"}],
            evidence_after=[{"c": "2"}],
            scopes={READ_SCOPE},
        )
        b = s.generate(
            namespace,
            p["policy_revision_id"],
            preview,
            principal_id="w",
            scopes={WRITE_SCOPE},
        )
        assert s.history(namespace, scopes={READ_SCOPE}, limit=1)["items"]
        assert s.export(namespace, [b["brief_id"]], scopes={READ_SCOPE})[
            "dependency_complete"
        ]
        assert (
            s.feedback(
                namespace,
                b["brief_id"],
                "useful",
                "material",
                principal_id="reviewer",
                scopes={REVIEW_SCOPE},
            )["rating"]
            == "useful"
        )
    with pytest.raises(ChangeBriefError, match="missing required scope"):
        s.history("economic", scopes={"knowledge:read"})
    with pytest.raises(ChangeBriefError, match="not found"):
        s.get(
            "political",
            s.history("economic", scopes={READ_SCOPE})["items"][0]["brief_id"],
            scopes={READ_SCOPE},
        )
    c.close()
