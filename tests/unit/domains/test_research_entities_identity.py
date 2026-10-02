"""Organisations and CORDIS participants matched through reviewable identity; researchers never matched (#2614)."""

from __future__ import annotations

import pytest

from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from src.kb.research_entities_identity import ResearchEntityIdentity
from src.kb.research_entities_records import ResearchEntityError
from tests.unit import research_entities_harness as h

PARTICIPANT = "research-entities:cordis-participant:"


def world(ownership=True):
    conn = h.connection()
    h.load_all(conn)
    if ownership:
        h.load_ownership(conn)
    identity = ResearchEntityIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                ownership_namespace=h.OWN_NS if ownership else None)
    return conn, identity, proposed


def pair(views, subject, target):
    return next(v for v in views if v["subject_key"] == subject and v["target_key"] == target)


def test_published_identifiers_are_proposed_before_names_and_nothing_is_accepted():
    _conn, identity, proposed = world()
    views = proposed["candidates"]
    assert {v["state"] for v in views} == {"proposed"}
    isni = pair(views, "research-entities:ror:0zzexa101", "lei:5299EXAMPLAUNIV00001")
    assert (isni["basis"], isni["method"], isni["low_evidence"]) == ("exact-identifier", "published-identifier", False)
    assert isni["evidence"][0]["identifiers"] == [{"scheme": "isni", "value": "0000000499990101"}]
    vat = pair(views, PARTICIPANT + h.PIC_INDUSTRIES, "lei:5299EXAMPLEINDGMBH01")
    assert vat["basis"] == "exact-identifier" and vat["confidence"] == 0.95
    domain = pair(views, PARTICIPANT + h.PIC_EXAMPLA, "research-entities:ror:0zzexa101")
    assert domain["method"] == "website-domain" and domain["evidence"][0]["hosts"] == ["exampla-university.example"]
    name = pair(views, PARTICIPANT + h.PIC_EXAMPLA, "lei:5299EXAMPLAUNIV00001")
    assert name["low_evidence"] and name["method"] == "name-country" and name["confidence"] < domain["confidence"]
    assert not [v for v in views if "orcid" in v["subject_key"] or "orcid" in v["target_key"]]
    assert "never proposed" in proposed["researchers"]
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == [] and len(again["candidates"]) == len(views)  # idempotent


def test_review_accept_reject_and_revert_are_recorded_decisions_and_records_stay_separate():
    conn, identity, proposed = world()
    views = proposed["candidates"]
    domain = pair(views, PARTICIPANT + h.PIC_EXAMPLA, "research-entities:ror:0zzexa101")
    with pytest.raises(ResearchEntityError):
        identity.review(h.NS, domain["candidate_id"], "accept", "ok", principal_id="alice", scopes=h.SCOPES)
    accepted = identity.review(h.NS, domain["candidate_id"], "accept", "same website, reviewed",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["decision_id"]
    assert [a["candidate_id"] for a in identity.accepted(h.NS, scopes=h.SCOPES, key=PARTICIPANT + h.PIC_EXAMPLA)] == [
        domain["candidate_id"]]
    name = pair(views, PARTICIPANT + h.PIC_EXAMPLA, "lei:5299EXAMPLAUNIV00001")
    rejected = identity.review(h.NS, name["candidate_id"], "reject", "a name alone", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert rejected["state"] == "rejected"
    reverted = identity.revert(h.NS, domain["candidate_id"], "second look", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert not identity.accepted(h.NS, scopes=h.SCOPES)
    assert conn.execute("SELECT count(*) FROM research_entity_records").fetchone()[0] == 11
    with pytest.raises(ResearchEntityError):
        identity.review(h.NS, "own-idc:unknown", "accept", "x", principal_id="reviewer", scopes=h.REVIEW_SCOPES)


def test_unmatched_records_stay_visible_and_matches_never_regroup_ownership_entities():
    conn, identity, proposed = world()
    unmatched = {u["key"] for u in proposed["unmatched"]}
    assert {"research-entities:ror:0zzexa202", "research-entities:ror:0zzexa505",
            PARTICIPANT + h.PIC_NORTHWIND} <= unmatched
    for view in proposed["candidates"]:
        identity.review(h.NS, view["candidate_id"], "accept", "fixture", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)
    assert "research-entities:" in FOREIGN_KEY_PREFIXES
    clusters = OwnershipIdentityService(conn, initialize=False).clusters(h.NS)
    assert not [k for k in clusters if k.startswith("lei:")]
    conflicts = identity.conflicts(h.NS, scopes=h.SCOPES)
    assert conflicts == []  # one target per kind and subject in the fixture world


def test_without_the_ownership_provider_participants_and_organisations_still_match_each_other():
    _conn, identity, proposed = world(ownership=False)
    assert proposed["ownership"]["status"] == "not_requested"
    assert {v["target_kind"] for v in proposed["candidates"]} == {"ror_organisation"}
    absent = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert absent["ownership"]["status"] == "provider_absent"
