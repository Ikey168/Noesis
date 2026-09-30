"""Reviewable identity for committees, candidates, parties and organisational donors (#2503)."""

from __future__ import annotations

import json

import pytest

from src.kb.campaign_finance_identity import CampaignFinanceIdentity
from src.kb.campaign_finance_records import CampaignFinanceError
from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from tests.unit import campaign_finance_harness as h


def loaded(version="v1"):
    conn = h.connection()
    h.load_all(conn)
    if version == "v2":
        h.apply(conn, "uk-ec-donations", version="v2", run_id="run:v2")
    h.load_ownership(conn)
    h.load_lobbying(conn)
    h.load_elections(conn)
    return conn


def by_pair(candidates):
    return {tuple(c["records"]): c for c in candidates}


def test_proposals_carry_method_evidence_and_confidence_and_nothing_is_accepted():
    conn = loaded("v2")
    identity = CampaignFinanceIdentity(conn)
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert result["unavailable"] == []
    found = {(c["records"][0], c["records"][1]): c for c in result["candidates"]}
    methods = {pair: c["method"] for pair, c in found.items()}

    def method(a, b):
        return methods.get((a, b)) or methods.get((b, a))

    assert method("campaign-finance:fec:committee:C00999902",
                  "campaign-finance:fec:donor-committee:C00999902") == "official-id"
    alex = next(c for pair, c in found.items() if "campaign-finance:fec:candidate:P99000001" in pair)
    assert alex["method"] == "name+office+cycle" and alex["basis"] == "name-jurisdiction"
    assert alex["evidence"][0]["election_id"] == "us-us-president:2099"
    assert method("campaign-finance:fec:connected-org:C00999902", "lei:5299EXAMPLEINDUSTR01") == "name+address"
    assert method("campaign-finance:fec:donor:example-industries-energy-llc:ex",
                  "lei:5299EXAMPLEENERGY001") == "name+address"
    assert method("campaign-finance:ukec:donor:77001", "gb-coh:09990002") == "company-number"
    assert method("campaign-finance:fec:donor:sample-utilities-association:ex",
                  "lobbying:us-lda:9002-8002") == "name+address"
    assert method("campaign-finance:ukec:donor:77009", "lobbying:uk-orcl:ORCL0099") == "company-number"
    party = next(c for pair, c in found.items() if "campaign-finance:ukec:entity:9901" in pair)
    assert party["method"] == "name+jurisdiction" and "party:example-party" in json.dumps(party["records"])
    assert all(c["state"] == "proposed" for c in result["candidates"])  # nothing auto-merged
    assert all(c["confidence"] > 0 and c["evidence"] for c in result["candidates"])
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == []  # idempotent


def test_individuals_are_never_subjects_and_unmatched_donors_stay_visible():
    conn = loaded()
    identity = CampaignFinanceIdentity(conn)
    subjects = identity.subjects(h.NS, scopes=h.SCOPES)
    text = json.dumps(subjects)
    assert "PLACEHOLDER" not in text and "MOCK, JORDAN" not in text
    assert not [s for s in subjects if s["kind"] not in {"candidate", "committee", "connected-organisation",
                                                          "regulated-entity", "donor-committee",
                                                          "donor-organisation"}]
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert not [c for c in result["candidates"] if "natural" in json.dumps(c["evidence"])]
    unmatched = identity.unmatched(h.NS, scopes=h.SCOPES)
    keys = {u["record_key"] for u in unmatched["unmatched"]}
    assert "campaign-finance:fec:donor-committee:C00999909" in keys  # a conduit never acquired: unmatched
    assert unmatched["individual_items_never_matched"] == 6  # 3 receipts, a payee, an IE payee, a UK donor
    assert "never matched" in unmatched["notice"]


def test_review_accept_and_revert_are_entity_identity_decisions():
    conn = loaded()
    identity = CampaignFinanceIdentity(conn)
    candidates = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                  ownership_namespace=h.OWN_NS)["candidates"]
    (energy,) = [c for c in candidates if "lei:5299EXAMPLEENERGY001" in c["records"]]
    with pytest.raises(Exception):
        identity.review(h.NS, energy["candidate_id"], "accept", "same company", principal_id="bob",
                        scopes=h.SCOPES)  # review scope required
    accepted = identity.review(h.NS, energy["candidate_id"], "accept", "name and state agree with the LEI record",
                               principal_id="bob", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    donor = "campaign-finance:fec:donor:example-industries-energy-llc:ex"
    assert identity.identity(h.NS, donor, scopes=h.SCOPES)["state"] == "matched"
    assert identity.accepted(h.NS, donor, scopes=h.SCOPES)[0]["record_key"] == "lei:5299EXAMPLEENERGY001"
    # ownership graph clusters ignore the foreign owner's link: records are never regrouped
    assert "campaign-finance:" in FOREIGN_KEY_PREFIXES
    assert "lei:5299EXAMPLEENERGY001" not in OwnershipIdentityService(conn).clusters(h.NS)
    reverted = identity.revert(h.NS, energy["candidate_id"], "second review", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    assert identity.identity(h.NS, donor, scopes=h.SCOPES)["state"] == "unmatched"
    with pytest.raises(CampaignFinanceError):
        identity.review(h.NS, "own-idc:not-ours", "accept", "x", principal_id="bob", scopes=h.REVIEW_SCOPES)


def test_missing_providers_are_reported_not_hidden():
    conn = h.connection()
    h.load_all(conn)
    result = CampaignFinanceIdentity(conn).propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    providers = {u["provider"] for u in result["unavailable"]}
    assert providers == {"political.elections", "ownership.core", "political.lobbying"}
    assert [c["method"] for c in result["candidates"]] == ["official-id"]
