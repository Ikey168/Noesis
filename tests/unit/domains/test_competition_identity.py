"""Case parties and aid beneficiaries matched to ownership entities through reviewable identity (#2217, CS07)."""

from __future__ import annotations

import pytest

from src.kb.competition import CompetitionError
from src.kb.competition_identity import CompetitionIdentity
from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from tests.unit import competition_harness as h

HOLD = "gleif:lei:213800EXAMPLAHOLDS95"
INT_BODS = "open-ownership:statement:oo-fixture-ent-int-1"
INT_GLEIF = "gleif:lei:724500EXAMPLAINTBV75"
SEC_HOLD = "sec-edgar:cik:0009999101"
DECOY = "open-ownership:statement:oo-fixture-ent-decoy-1"


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.ownership(conn)
    h.load_all(conn)
    identity = CompetitionIdentity(conn)
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    return conn, identity, proposed


def by_subject(views, subject_prefix):
    return [v for v in views if v["subject_key"].startswith(subject_prefix)]


def test_identifier_candidates_come_first_and_names_are_low_evidence(world):
    _, identity, proposed = world
    views = proposed["candidates"]
    assert views and all(v["state"] == "proposed" for v in views)  # nothing is accepted automatically
    award = {v["ownership_key"]: v for v in views if v["subject_key"] == h.AWARD_INT}
    assert award[INT_BODS]["method"] == award[INT_GLEIF]["method"] == "exact-identifier"
    assert award[INT_BODS]["low_evidence"] is False
    assert award[INT_BODS]["evidence"][0]["identifiers"][0]["value"] == "99990003"
    ec_party = [v for v in views if v["subject_key"].startswith("competition:party:ec:M.99001")
                and v["ownership_key"] == HOLD]
    assert ec_party and ec_party[0]["method"] == "name-jurisdiction" and ec_party[0]["low_evidence"] is True
    assert ec_party[0]["evidence"][0]["country_basis"] == "stated on both"
    cma = [v for v in views if v["subject_key"].startswith("competition:party:uk-cma")]
    assert cma and all(v["evidence"][0]["country_basis"] == "not published for the party" for v in cma)
    # The TAM award without a national identifier only has name candidates.
    assert {v["method"] for v in views if v["subject_key"] == h.AWARD_OTHER} == {"name-jurisdiction"}
    assert "competition:" in FOREIGN_KEY_PREFIXES


def test_unmatched_parties_stay_as_published(world):
    _, identity, _ = world
    unmatched = {u["name_as_published"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert {"Northwind Widgets GmbH", "Northwind Widgets, Inc.", "Northwind Energie B.V."} <= unmatched
    northwind = [u for u in identity.unmatched(h.NS, scopes=h.SCOPES) if u["name_as_published"].startswith("Northwind")]
    assert all(u["pending_candidates"] == 0 and u["status"] == "unmatched" for u in northwind)


def test_one_party_several_entities_is_a_conflict_until_reviewed(world):
    conn, identity, proposed = world
    conflicts = {c["subject_key"]: c for c in identity.conflicts(h.NS, ownership_namespace=h.OWN_NS,
                                                                 scopes=h.SCOPES)}
    ec_party = next(v["subject_key"] for v in proposed["candidates"] if v["ownership_key"] == HOLD
                    and v["subject_key"].startswith("competition:party:ec:"))
    # The SEC record and the same-name decoy stand in their own ownership clusters.
    assert {g["cluster"] for g in conflicts[ec_party]["clusters"]} == {
        "companies-house:gb-coh:09990001", DECOY, SEC_HOLD}
    for other in (SEC_HOLD, DECOY):
        wrong = next(v for v in proposed["candidates"] if v["subject_key"] == ec_party and v["ownership_key"] == other)
        identity.review(h.NS, wrong["candidate_id"], "reject", "no register number corroborates it",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert ec_party not in {c["subject_key"] for c in identity.conflicts(h.NS, ownership_namespace=h.OWN_NS,
                                                                         scopes=h.SCOPES)}


def test_review_records_reviewer_and_time_and_is_reversible(world):
    conn, identity, proposed = world
    candidate = next(v for v in proposed["candidates"] if v["subject_key"] == h.AWARD_INT
                     and v["ownership_key"] == INT_BODS)
    accepted = identity.review(h.NS, candidate["candidate_id"], "accept", "KvK number matches",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["reviewed_at_ms"]
    assert accepted["decision_id"]
    links = identity.accepted_links(h.NS, {INT_BODS}, scopes=h.SCOPES)
    assert [link["subject_key"] for link in links] == [h.AWARD_INT]
    assert h.AWARD_INT not in {u["key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    reverted = identity.revert(h.NS, candidate["candidate_id"], "re-check the register", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and not identity.accepted_links(h.NS, {INT_BODS}, scopes=h.SCOPES)
    # The ownership namespace's own clusters are never regrouped by a competition link.
    assert all(not k.startswith("competition:") for k in OwnershipIdentityService(conn).clusters(h.OWN_NS))
    with pytest.raises(CompetitionError):
        identity.review(h.NS, "own-idc:not-a-competition-candidate", "accept", "x", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)


def test_reproposing_is_idempotent_and_needs_scopes(world):
    _, identity, _ = world
    again = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert again["proposed"] == []
    with pytest.raises(CompetitionError):
        identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.READ_ONLY)
