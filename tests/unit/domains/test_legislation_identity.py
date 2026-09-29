"""Sponsors, cosponsors and voting members matched through reviewable identity (#2424)."""

from __future__ import annotations

import pytest

from src.domains.political.model import record_alias, record_object
from src.kb.legislation import LegislationError
from src.kb.legislation_identity import LegislationIdentity
from src.kb.ownership_store import OwnershipError
from tests.unit import elections_harness as eh
from tests.unit import legislation_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    eh.apply(connection, "gb", eh.UK)  # UK general election candidates (fictional)
    yield connection
    connection.close()


def test_members_are_official_identifiers_with_party_and_place_as_of_each_record(conn):
    identity = LegislationIdentity(conn, now=h.Clock())
    members = {m["member_key"]: m for m in identity.members(h.NS, scopes=h.SCOPES)}
    sample = members["legislation:member:us-bioguide:S009901"]
    assert sample["names"] == ["Jane Sample"] and sample["jurisdiction"] == "US"
    roles = {(a["role"], a["record_kind"]) for a in sample["appearances"]}
    assert ("sponsor", "us-bill") in roles and ("voter", "us-roll-call") in roles
    vote = next(a for a in sample["appearances"] if a["role"] == "voter")
    assert (vote["party"], vote["state"], vote["position"], vote["date"]) == ("D", "CA", "Yea", "2099-03-10")
    placeholder = members["legislation:member:us-bioguide:P009903"]
    cosponsor = next(a for a in placeholder["appearances"] if a["record_kind"] == "us-bill")
    assert cosponsor["withdrawn_date"] == "2099-03-01"
    # Senate LIS ids are their own external identifiers, never folded into a bioguide ID by name
    assert "legislation:member:us-lis:S902" in members
    minister = members["legislation:member:uk-parliament:4001"]
    assert {a["role"] for a in minister["appearances"]} == {"sponsor", "voter", "contributor"}
    assert next(a for a in minister["appearances"] if a["role"] == "voter")["constituency"] == "Exampleton North"


def test_candidates_are_proposed_reviewed_and_reverted_never_merged(conn):
    record_object(conn, object_id="person:us:jane-sample", object_type="person", canonical_name="Jane Sample",
                  jurisdiction_id="us")
    record_alias(conn, object_id="person:us:jane-sample", alias="Jane Sample", jurisdiction_id="us")
    identity = LegislationIdentity(conn, now=h.Clock())
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                political_jurisdictions={"US": "us", "GB": "gb"})
    views = {tuple(sorted(v["records"])): v for v in proposed["candidates"]}
    election = next(v for k, v in views.items() if "legislation:member:uk-parliament:4002" in k)
    assert election["method"] == "name-jurisdiction" and election["state"] == "proposed"
    assert election["confidence"] < 0.5
    evidence = election["evidence"][0]
    assert evidence["left"]["external_identifier"] == {"scheme": "uk-parliament", "value": "4002"}
    assert evidence["kind"] == "election-candidate"
    alias = next(v for k, v in views.items() if "political:person:us:jane-sample" in k)
    assert alias["evidence"][0]["kind"] == "political-alias"
    # idempotent
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                             political_jurisdictions={"US": "us", "GB": "gb"})
    assert again["proposed"] == []
    with pytest.raises(OwnershipError):
        identity.review(h.NS, election["candidate_id"], "accept", "same member", principal_id="rev",
                        scopes=h.SCOPES)
    accepted = identity.review(h.NS, election["candidate_id"], "accept", "constituency and party agree",
                               principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    member = "legislation:member:uk-parliament:4002"
    assert identity.identity(h.NS, member, scopes=h.SCOPES)["state"] == "matched"
    # the records themselves are untouched: the member keeps its own identifier and names
    assert any(m["member_key"] == member for m in identity.members(h.NS, scopes=h.SCOPES))
    reverted = identity.revert(h.NS, election["candidate_id"], "different person", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    assert identity.identity(h.NS, member, scopes=h.SCOPES)["state"] == "unmatched"


def test_unmatched_members_stay_visible(conn):
    identity = LegislationIdentity(conn, now=h.Clock())
    identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    unmatched = {m["member_key"] for m in identity.unmatched(h.NS, scopes=h.SCOPES, bill_key=h.UK_BILL)}
    assert "legislation:member:uk-parliament:4003" in unmatched
    assert "legislation:member:uk-parliament:4002" in unmatched  # proposed, not yet accepted
    with pytest.raises(LegislationError) as foreign:
        identity.review(h.NS, "own-idc:not-ours", "accept", "x", principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert foreign.value.code == "not_found"
