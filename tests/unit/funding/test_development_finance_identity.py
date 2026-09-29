"""Reviewable organisation identity and the open-call cross-reference (#2032)."""

from __future__ import annotations

import pytest

from src.kb.development_finance import DevelopmentFinanceStore
from src.kb.development_finance_identity import (
    DevelopmentFinanceIdentity,
    funder_key,
    register_number,
    subject_key,
)
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_store import OwnershipError
from tests.unit.funding import development_finance_harness as h

FDPA_ID = "iati:ref:XM-DAC-99901"
NGO_ID = "iati:ref:XI-IATI-FICTNGO"
WATER_WORKS = subject_key(FDPA_ID, "GB-COH-99000001", "Fictional Water Works Ltd")
TRUST = subject_key(FDPA_ID, None, "Fictional Learning Trust")


@pytest.fixture()
def env():
    env = h.Env().load()
    h.seed_ownership(env.conn)
    h.seed_open_call(env.conn, now=env.now())
    yield env
    env.conn.close()


def _snapshot(conn):
    return conn.execute(
        "SELECT revision_id, body_json FROM devfin_activity_revisions ORDER BY 1"
    ).fetchall()


def test_register_prefixes_are_compared_after_one_normalisation():
    assert register_number("GB-COH-99000001") == ("gb-coh", "99000001")
    assert register_number(" gb-coh-99000001 ") == ("gb-coh", "99000001")
    assert register_number("XM-DAC-99901") is None


def test_propose_review_and_undo_leave_the_activity_records_untouched(env):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    before = _snapshot(env.conn)
    result = identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.NS
    )
    pairs = {tuple(sorted(c["records"])): c for c in result["candidates"]}
    companies = pairs[tuple(sorted((WATER_WORKS, "companies-house:99000001")))]
    assert (
        companies["basis"] == "cross-referenced-identifier"
        and companies["state"] == "proposed"
    )
    assert identity.identity(h.NS, WATER_WORKS, scopes=h.SCOPES)["state"] == "unmatched"
    accepted = identity.service.review(
        h.NS,
        companies["candidate_id"],
        "accept",
        "same Companies House number",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert identity.view(accepted)["review_state"] == "reviewed-match"
    matched = identity.identity(h.NS, WATER_WORKS, scopes=h.SCOPES)
    assert (
        matched["state"] == "matched"
        and matched["links"][0]["target"] == "companies-house:99000001"
    )
    decision = env.conn.execute(
        "SELECT count(*) FROM entity_identity_decisions"
    ).fetchone()[0]
    assert decision >= 1
    # Development-finance links never regroup ownership entities.
    assert WATER_WORKS not in OwnershipIdentityService(env.conn).clusters(h.NS)
    identity.service.revert(
        h.NS,
        companies["candidate_id"],
        "wrong register entry",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert identity.identity(h.NS, WATER_WORKS, scopes=h.SCOPES)["state"] == "unmatched"
    again = identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.NS
    )
    assert (
        again["proposed"] == []
    )  # a reverted candidate is not re-proposed on the same evidence
    assert _snapshot(env.conn) == before


def test_an_organisation_without_a_reference_stays_a_source_string_even_with_two_same_named_entities(
    env,
):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.NS
    )
    trust = identity.identity(h.NS, TRUST, scopes=h.SCOPES)
    assert (
        trust["state"] == "unmatched"
        and trust["links"] == []
        and trust["pending"] == []
    )
    subjects = {s["record_key"]: s for s in identity.subjects(h.NS)}
    assert (
        subjects[TRUST]["name"] == "Fictional Learning Trust"
        and subjects[TRUST]["ref"] is None
    )


def test_ambiguous_candidates_stay_unmatched(env):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    # Two ownership entities carry the same stated number (a register duplicate): both are proposed, neither linked.
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    OwnershipStore(env.conn).apply(
        h.NS,
        [
            record(
                "legal_entity",
                "bods:entity:water-works-copy",
                {"provider": "open-ownership", "provider_record_id": "s9"},
                name="Fictional Water Works Limited",
                jurisdiction="GB",
                identifiers=[{"scheme": "gb-coh", "value": "99 000 001"}],
            )
        ],
        run_id="own2",
        observed_at_ms=2,
        principal_id="p",
    )
    identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.NS
    )
    state = identity.identity(h.NS, WATER_WORKS, scopes=h.SCOPES)
    assert (
        state["state"] == "ambiguous"
        and len(state["pending"]) == 2
        and state["links"] == []
    )


def test_publishers_are_never_merged_and_a_funder_query_reaches_both_publishers(env):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    result = identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, funding_namespace="grants"
    )
    ngo_funder = subject_key(NGO_ID, h.FDPA, "Fictional Development Partnership Agency")
    fdpa_funder = subject_key(
        FDPA_ID, h.FDPA, "Fictional Development Partnership Agency"
    )
    to_publisher = [
        c for c in result["candidates"] if "devfin:publisher:" + FDPA_ID in c["records"]
    ]
    assert {
        c["left_key"]
        if c["right_key"].startswith("devfin:publisher:")
        else c["right_key"]
        for c in to_publisher
    } == {ngo_funder, fdpa_funder}
    for candidate in to_publisher:
        identity.service.review(
            h.NS,
            candidate["candidate_id"],
            "accept",
            "the reported reference is the publisher's",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    reached = identity.linked_subjects(
        h.NS, "devfin:publisher:" + FDPA_ID, scopes=h.SCOPES
    )
    assert reached == sorted([fdpa_funder, ngo_funder])
    store = DevelopmentFinanceStore(env.conn)
    assert len(store.publishers(h.NS)) == 4  # no publisher record merged or removed


def test_a_funder_with_activities_and_an_open_call(env):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    result = identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, funding_namespace="grants"
    )
    fdpa_funder = subject_key(
        FDPA_ID, h.FDPA, "Fictional Development Partnership Agency"
    )
    (call_candidate,) = [
        c
        for c in result["candidates"]
        if funder_key(h.FDPA) in c["records"] and fdpa_funder in c["records"]
    ]
    assert call_candidate["basis"] == "cross-referenced-identifier"
    assert (
        identity.open_calls(
            h.NS, fdpa_funder, funding_namespace="grants", scopes=h.SCOPES
        )
        == []
    )
    identity.service.review(
        h.NS,
        call_candidate["candidate_id"],
        "accept",
        "funder id is the IATI reference",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    (call,) = identity.open_calls(
        h.NS, fdpa_funder, funding_namespace="grants", scopes=h.SCOPES
    )
    assert call["record_kind"] == "call" and call["revision"] == 1
    assert (
        "never an activity, a transaction or a commitment" in call["note"]
        and "eligib" in call["note"]
    )
    assert "eligibility" not in {k for k in call if k != "note"}


def test_reviews_need_the_review_scope_and_similar_names_are_never_accepted(env):
    from src.kb.entities import add_manual_alias

    add_manual_alias(
        env.conn,
        "Fictional Development Partnership Agency",
        "Fictional Development Partnership Agency",
        "ORG",
    )
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    names = [c for c in result["candidates"] if c["basis"] == "similar-name"]
    assert names
    with pytest.raises(OwnershipError):
        identity.service.review(
            h.NS,
            names[0]["candidate_id"],
            "accept",
            "looks similar",
            principal_id="r",
            scopes=h.SCOPES,
        )
    with pytest.raises(OwnershipError) as never:
        identity.service.review(
            h.NS,
            names[0]["candidate_id"],
            "accept",
            "looks similar",
            principal_id="r",
            scopes=h.REVIEW_SCOPES,
        )
    assert never.value.code == "insufficient_evidence"
