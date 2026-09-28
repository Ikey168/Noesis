"""Reviewable subject matches to Products models and canonical entities (ES10, #2071)."""

from __future__ import annotations

import pytest

from src.kb.engineering_safety_identity import SubjectIdentity, query_keys
from src.kb.engineering_safety_records import EngineeringSafetyError
from tests.unit.engineering_safety import harness as h


@pytest.fixture()
def env():
    env = h.Env()
    assert env.run("r1")["status"] == "complete"
    h.seed_products(env.conn)
    h.seed_entities(env.conn)
    return env


def identity(env):
    return SubjectIdentity(env.conn, now=lambda: next(env.clock))


def by_target(result, target, key=None):
    return [
        c
        for c in result["candidates"]
        if c["target_id"] == target and (key is None or c["subject_key"] == key)
    ]


V2025 = "vehicle:velomark:cityrunner:2025"


def test_candidates_are_deterministic_with_evidence_and_never_accepted(env):
    ids = identity(env)
    first = ids.propose(
        h.NS,
        scopes=h.REVIEW | h.PRODUCTS,
        products_namespace=h.NS,
        principal_id="matcher",
    )
    again = ids.propose(
        h.NS,
        scopes=h.REVIEW | h.PRODUCTS,
        products_namespace=h.NS,
        principal_id="matcher",
    )
    assert [c["match_id"] for c in first["candidates"]] == [
        c["match_id"] for c in again["candidates"]
    ]
    assert again["changes"] == []  # idempotent
    years = {
        c["subject_key"] for c in by_target(first, "product-model:velomark-cityrunner")
    }
    assert years == {
        V2025,
        "vehicle:velomark:cityrunner:2026",
    }  # one candidate per published model year
    (vehicle,) = by_target(first, "product-model:velomark-cityrunner", V2025)
    assert (
        vehicle["basis"] == "designation+brand"
        and vehicle["review_state"] == "unreviewed"
    )
    assert not vehicle["attached"] and vehicle["evidence"]["compared"]["brand"] == [
        "VELOMARK",
        "VELOMARK",
    ]
    assert {r["native_id"] for r in vehicle["evidence"]["records"]} >= {
        "PE26003",
        "EA26002",
    }
    # No sibling ("CITYRUNNER X") and no other brand is ever proposed.
    assert not by_target(first, "product-model:velomark-cityrunner-x")
    assert not by_target(first, "product-model:otherbrand-cityrunner")
    (operator,) = by_target(first, "ent-examplar-pipeline")
    assert (
        operator["basis"] == "canonical-alias"
        and operator["subject_key"] == "pipeline-operator:39999"
    )


def test_accept_reject_and_revert_are_appended_and_never_reactivate(env):
    ids = identity(env)
    proposed = ids.propose(
        h.NS,
        scopes=h.REVIEW | h.PRODUCTS,
        products_namespace=h.NS,
        principal_id="matcher",
    )
    (operator,) = by_target(proposed, "ent-examplar-pipeline")
    with pytest.raises(EngineeringSafetyError) as denied:
        ids.review(
            h.NS,
            operator["match_id"],
            "accepted",
            "same operator",
            scopes=h.WRITE,
            principal_id="r",
        )
    assert denied.value.code == "unauthorized"
    accepted = ids.review(
        h.NS,
        operator["match_id"],
        "accepted",
        "register name equals",
        scopes=h.REVIEW,
        principal_id="reviewer",
    )
    assert accepted["attached"] and accepted["review_history"][0]["decision_id"]
    decision = env.conn.execute(
        "SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
        [accepted["review_history"][0]["decision_id"]],
    ).fetchone()
    assert decision == ("match",)
    with pytest.raises(EngineeringSafetyError):
        ids.review(
            h.NS,
            operator["match_id"],
            "rejected",
            "again",
            scopes=h.REVIEW,
            principal_id="reviewer",
        )
    reverted = ids.revert(
        h.NS,
        operator["match_id"],
        "checked the register",
        scopes=h.REVIEW,
        principal_id="r",
    )
    assert reverted["review_state"] == "reverted" and not reverted["attached"]
    assert [r["decision"] for r in reverted["review_history"]] == [
        "accepted",
        "reverted",
    ]
    (vehicle,) = by_target(proposed, "product-model:velomark-cityrunner", V2025)
    rejected = ids.review(
        h.NS,
        vehicle["match_id"],
        "rejected",
        "different market",
        scopes=h.REVIEW,
        principal_id="reviewer",
    )
    assert rejected["review_state"] == "rejected" and not rejected["attached"]


def test_stronger_evidence_upgrades_and_a_source_revision_reproposes(env):
    ids = identity(env)
    # A pending designation-only candidate is upgraded in place when a revision states the maker.
    statement = {
        "contract": "noesis-engineering-safety-record-v1",
        "provider": "nhtsa-complaints",
        "record_kind": "complaint",
        "native_id": "11800999",
        "authority": {"code": "us-nhtsa-odi", "declared": "NHTSA"},
        "revision_date": "2026-03-01",
        "subjects": [
            {
                "kind": "component",
                "fields": {"part_number": "CITYRUNNER"},
                "source_string": "CITYRUNNER",
            }
        ],
    }
    env.store.apply(h.NS, statement)
    first = ids.propose(
        h.NS, scopes=h.REVIEW | h.PRODUCTS, products_namespace=h.NS, principal_id="m"
    )
    (weak,) = [
        c
        for c in by_target(first, "product-model:velomark-cityrunner")
        if c["subject_key"].startswith("component:")
    ]
    assert weak["basis"] == "designation-only"
    env.store.apply(
        h.NS,
        {
            **statement,
            "revision_date": "2026-03-02",
            "subjects": [
                {
                    "kind": "component",
                    "fields": {"part_number": "CITYRUNNER", "manufacturer": "VELOMARK"},
                    "source_string": "VELOMARK CITYRUNNER",
                }
            ],
        },
    )
    upgraded = ids.propose(
        h.NS, scopes=h.REVIEW | h.PRODUCTS, products_namespace=h.NS, principal_id="m"
    )
    detached = next(
        c for c in upgraded["candidates"] if c["match_id"] == weak["match_id"]
    )
    assert (
        detached["candidate_state"] == "not_named_in_current_revision"
    )  # its key is no longer named
    strong = [
        c
        for c in by_target(upgraded, "product-model:velomark-cityrunner")
        if c["subject_key"] == "component:velomark:cityrunner"
    ]
    assert strong and strong[0]["basis"] == "designation+brand"
    # A rejected candidate named again after being detached is proposed afresh and needs a new review.
    ids.review(
        h.NS,
        strong[0]["match_id"],
        "rejected",
        "not this part",
        scopes=h.REVIEW,
        principal_id="r",
    )
    env.store.apply(h.NS, {**statement, "revision_date": "2026-03-03"})
    ids.propose(
        h.NS, scopes=h.REVIEW | h.PRODUCTS, products_namespace=h.NS, principal_id="m"
    )
    env.store.apply(
        h.NS,
        {
            **statement,
            "revision_date": "2026-03-04",
            "subjects": [
                {
                    "kind": "component",
                    "fields": {"part_number": "CITYRUNNER", "manufacturer": "VELOMARK"},
                    "source_string": "VELOMARK CITYRUNNER",
                }
            ],
        },
    )
    renamed = ids.propose(
        h.NS, scopes=h.REVIEW | h.PRODUCTS, products_namespace=h.NS, principal_id="m"
    )
    again = next(
        c for c in renamed["candidates"] if c["match_id"] == strong[0]["match_id"]
    )
    assert (
        again["candidate_state"] == "proposed" and again["review_state"] == "unreviewed"
    )
    assert again["needs_re_review"] and again["proposal_seq"] == 2


def test_lookups_share_one_equivalence_and_unmatched_subjects_stay_strings(env):
    ids = identity(env)
    proposed = ids.propose(
        h.NS, scopes=h.REVIEW | h.PRODUCTS, products_namespace=h.NS, principal_id="m"
    )
    (vehicle,) = by_target(proposed, "product-model:velomark-cityrunner", V2025)
    assert (
        query_keys(
            ids, h.NS, {"product_model_id": "product-model:velomark-cityrunner"}
        )[0]
        == []
    )
    ids.review(
        h.NS,
        vehicle["match_id"],
        "accepted",
        "make and model agree",
        scopes=h.REVIEW,
        principal_id="r",
    )
    patterns, matches = query_keys(
        ids, h.NS, {"product_model_id": "product-model:velomark-cityrunner"}
    )
    assert patterns == [{"key": V2025, "prefix": False}]
    assert matches and matches[0]["attached"]
    assert query_keys(
        ids, h.NS, {"kind": "vehicle", "make": "Velomark", "model": "City-Runner"}
    )[0] == [{"key": "vehicle:velomark:cityrunner", "prefix": True}]
    with pytest.raises(EngineeringSafetyError):
        query_keys(ids, h.NS, {"kind": "vehicle", "model": "X"})


def test_operator_names_are_offered_into_the_ownership_state_machine_only_when_asked(
    env,
):
    ids = identity(env)
    with pytest.raises(EngineeringSafetyError) as denied:
        ids.propose(
            h.NS,
            scopes=h.REVIEW | h.PRODUCTS,
            products_namespace=h.NS,
            principal_id="m",
            ownership_namespace="own",
        )
    assert denied.value.code == "unauthorized"
    result = ids.propose(
        h.NS,
        scopes=h.ALL | {"namespace:own:read"},
        principal_id="m",
        ownership_namespace="own",
    )
    assert (
        result["ownership_candidates"] == []
    )  # no ownership records acquired: nothing is offered
