"""Reviewable identity for companies, commodities and projects (#2653, EX06 #2682)."""

from __future__ import annotations

import pytest

from src.kb.extractives_identity import ExtractivesIdentity
from src.kb.extractives_records import ExtractivesError
from src.kb.ownership_identity import OwnershipIdentityService
from tests.unit import extractives_harness as h


def test_without_ownership_records_every_company_stays_unmatched():
    conn = h.connection()
    h.load_all(conn)
    result = ExtractivesIdentity(conn).propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst",
                                                         scopes=h.SCOPES)
    assert result["status"] == "ownership_absent" and result["candidates"] == []
    assert {u["key"] for u in result["unmatched"]} == {h.INT_COMPANY, h.NORTHWIND, h.HOLD_COMPANY, h.UK_COMPANY}


def test_company_candidates_use_published_identifiers_first_names_as_low_evidence_and_nothing_is_accepted():
    conn = h.connection()
    h.ownership(conn)
    h.load_all(conn)
    identity = ExtractivesIdentity(conn)
    result = identity.propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    candidates = result["candidates"]
    assert candidates and {c["state"] for c in candidates} == {"proposed"}  # nothing auto-accepted
    by_subject: dict[str, set[str]] = {}
    for c in candidates:
        by_subject.setdefault(c["subject_key"], set()).add(c["method"])
        assert c["confidence"] and c["evidence"] and c["history"]
    # The KvK number the NL report publishes is the GLEIF RA000463 number: an exact identifier candidate.
    exact_int = [c for c in candidates if c["subject_key"] == h.INT_COMPANY and c["method"] == "exact-identifier"]
    assert h.INT_ENTITY in {c["ownership_key"] for c in exact_int}
    assert "exact-identifier" in by_subject[h.HOLD_COMPANY]
    # Exampla UK Limited is published without an identifier: name candidates only, low evidence.
    assert by_subject[h.UK_COMPANY] == {"name-jurisdiction"}
    assert all(c["low_evidence"] for c in candidates if c["subject_key"] == h.UK_COMPANY)
    # Northwind has no ownership record; the individual payer is never offered.
    assert h.NORTHWIND not in by_subject
    assert not any(":individual:" in c["subject_key"] for c in candidates)
    assert {u["key"] for u in result["unmatched"]} == {h.INT_COMPANY, h.NORTHWIND, h.HOLD_COMPANY, h.UK_COMPANY}
    # Re-proposing is idempotent.
    again = identity.propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert again["proposed"] == []


def test_review_accepts_rejects_and_reverts_with_reviewer_and_never_regroups_ownership_entities():
    conn = h.connection()
    state = h.reviewed(conn)
    identity = state["identity"]
    clusters_before = OwnershipIdentityService(conn).clusters(h.OWN_NS)
    accepted = [c for c in identity.company_candidates(h.NS, scopes=h.SCOPES) if c["state"] == "accepted"]
    assert accepted and all(c["reviewer"] == "reviewer" and c["decision_id"] for c in accepted)
    assert all(c["method"] == "exact-identifier" for c in accepted)
    unmatched = {u["key"]: u for u in identity.unmatched_companies(h.NS, scopes=h.SCOPES)}
    assert set(unmatched) == {h.NORTHWIND, h.UK_COMPANY} and unmatched[h.UK_COMPANY]["pending_candidates"] >= 1
    with pytest.raises(ExtractivesError):
        identity.review_company(h.NS, accepted[0]["candidate_id"], "accept", "again", principal_id="reviewer",
                                scopes=h.SCOPES)  # lacks the ownership review scope
    reverted = identity.revert_company(h.NS, accepted[0]["candidate_id"], "register number re-checked",
                                       principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and reverted["reason"] == "register number re-checked"
    # The ownership namespace's clusters are unchanged by extractives links.
    assert OwnershipIdentityService(conn).clusters(h.OWN_NS) == clusters_before


def test_commodities_map_to_hs_headings_only_through_a_recorded_published_concordance():
    conn = h.connection()
    h.load_all(conn)
    identity = ExtractivesIdentity(conn)
    first = identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert first["proposed"] == [] and {u["subject_key"] for u in first["unmatched"]} == {
        "commodity:copper", "commodity:crude-petroleum", "commodity:lithium"}
    with pytest.raises(ExtractivesError):
        identity.import_concordance(h.NS, {**h.CONCORDANCE, "citation": {"url": "http://x"}}, principal_id="op",
                                    scopes=h.SCOPES)
    recorded = identity.import_concordance(h.NS, h.CONCORDANCE, principal_id="op", scopes=h.SCOPES)
    assert recorded["created"] and not identity.import_concordance(h.NS, h.CONCORDANCE, principal_id="op",
                                                                   scopes=h.SCOPES)["created"]
    result = identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)
    matches = {m["subject_key"]: m for m in result["matches"]}
    assert set(matches) == {"commodity:copper", "commodity:crude-petroleum"}
    assert [u["subject_key"] for u in result["unmatched"]] == ["commodity:lithium"]  # never guessed
    copper = matches["commodity:copper"]
    assert copper["state"] == "proposed" and copper["method"] == "published-concordance"
    assert copper["target"]["id"] == "hs:HS2022:2603" and copper["evidence"]["citation"]["url"].startswith("https")
    assert identity.accepted_hs_codes(h.NS, "copper") == []
    identity.review(h.NS, copper["match_id"], "accept", "BGS table checked", principal_id="reviewer",
                    scopes=h.SCOPES)
    assert identity.accepted_hs_codes(h.NS, "copper")[0]["hs_code"] == "2603"
    reverted = identity.revert(h.NS, copper["match_id"], "edition to be re-checked", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.accepted_hs_codes(h.NS, "copper") == []
    with pytest.raises(ExtractivesError):
        identity.review(h.NS, copper["match_id"], "accept", "x", principal_id="reviewer", scopes=h.READ_ONLY)


def test_projects_map_to_infrastructure_only_through_published_identifiers_or_coordinates():
    conn = h.connection()
    h.load_all(conn)
    identity = ExtractivesIdentity(conn)
    absent = identity.propose_projects(h.NS, infrastructure_namespace="infra", principal_id="analyst",
                                       scopes=h.SCOPES)
    assert absent["status"] == "provider_absent" and len(absent["unmatched"]) == 2
    asset_id = h.seed_infrastructure(conn)
    result = identity.propose_projects(h.NS, infrastructure_namespace="infra", principal_id="analyst",
                                       scopes=h.SCOPES)
    (match,) = result["matches"]
    assert match["subject_key"] == f"{h.DE_REPORT}:project:p1" and match["target"]["id"] == asset_id
    assert match["method"] == "shared-identifier" and match["state"] == "proposed"
    assert match["evidence"]["identifiers"] == [{"scheme": "gem-mine-id", "value": "M9001"}]
    assert match["subject_revision_id"] and match["target"]["revision_id"]
    # The NL gas field publishes neither an identifier nor coordinates an asset shares: unmatched, not name-matched.
    assert [u["subject_key"] for u in result["unmatched"]] == [f"{h.NL_REPORT}:project:p1"]
