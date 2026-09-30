"""Enforcement records linked by citation, shared identifier or accepted match (#2651, EN08)."""

from __future__ import annotations

import pytest

from src.kb.enforcement_links import EnforcementLinks, parse_legal_bases
from tests.unit import enforcement_harness as h


def keys(text, **kwargs):
    return [c["key"] for c in parse_legal_bases(text, **kwargs)]


def test_exact_citations_are_parsed_at_their_fixed_codification():
    assert keys("Section 10(b) of the Securities Exchange Act of 1934 and Rule 10b-5 thereunder") == [
        "usc:15:78j", "cfr:17:240.10b-5"]
    assert keys("Section 17(a) of the Securities Act of 1933") == ["usc:15:77q"]
    assert keys("Section 206(2) of the Investment Advisers Act of 1940") == ["usc:15:80b-6"]
    assert keys("15 U.S.C. § 78m(a)") == ["usc:15:78m"]
    assert keys("Clean Air Act Section 112(r)") == ["us-act:clean-air-act:112(r)"]
    assert keys("Principle 3 of the Authority's Principles for Businesses and SYSC 6.1.1R") == [
        "fca-handbook:PRIN 2.1.1R:principle-3", "fca-handbook:SYSC 6.1.1R"]
    assert keys("section 206 of the Act") == ["uk:ukpga/2000/8:s206"]
    assert keys("Article 5(1)(f)", context="gdpr") == ["celex:32016R0679:art5"]
    assert keys("Article 5(1)(f)") == []  # a bare article is GDPR only in a GDPR context
    assert keys("reviewed a related transaction in case M.99001") == ["case:ec:M.99001"]
    assert keys("serious misconduct and a pattern of concern") == []  # nothing is linked by topic


@pytest.fixture()
def linked():
    conn = h.connection()
    state = h.reviewed(conn)
    yield conn, state
    conn.close()


def by(links, pack):
    return [link for link in links if link["target_pack"] == pack]


def test_links_record_their_basis_and_point_at_specific_revisions(linked):
    conn, state = linked
    listing = EnforcementLinks(conn).list_links(h.NS, scopes=h.SCOPES)
    links = listing["links"]
    assert {link["basis"] for link in links} == {"citation", "shared_identifier", "accepted_match"}
    assert all(link["citing_revision_id"].startswith("enf-rev:") for link in links)
    legal = {link["raw"]: link for link in by(links, "legal.works") if link["status"] == "resolved"}
    assert legal["Section 10(b) of the Securities Exchange Act of 1934"]["target_record"] == state["works"]["usc15"]
    assert legal["section 206 of the Act"]["target_record"] == state["works"]["fsma"]
    assert legal["Article 32"]["target_record"] == state["works"]["gdpr"]
    competition = by(links, "ownership.competition")
    assert [(c["raw"], c["status"], c["target_record"]) for c in competition] == [
        ("M.99001", "resolved", "competition:case:ec:M.99001")]
    assert competition[0]["target_revision"].startswith("own-rev:")
    filings = {link["raw"]: link for link in by(links, "market.filings")}
    assert filings["0009999101"]["status"] == "resolved" and filings["0009999101"]["basis"] == "shared_identifier"
    assert filings["0009999101"]["target_record"] == h.SEC_FILER
    assert filings["0009999202"]["status"] == "unresolved"  # Northwind has no filer record: reported, not dropped
    accepted = by(links, "ownership.core")
    assert accepted and all(link["basis"] == "accepted_match" and link["target_revision"] for link in accepted)
    # Missing providers and targets are reported, never dropped.
    assert listing["providers_unavailable"] == ["legal.courts"]
    assert {link["raw"] for link in listing["unresolved"]} >= {"SYSC 6.1.1R", "0009999202"}


def test_open_links_resolve_when_the_target_arrives_and_are_idempotent(linked):
    conn, _ = linked
    from src.kb.legal_dockets import LegalDocketStore

    LegalDocketStore(conn)
    conn.execute("INSERT INTO legal_docket_revisions (revision_id, namespace, work_id, version_id, record_key, "
                 "source_id, provider, court_id, courtlistener_docket_id, docket_number, content_sha256, revision_no, "
                 "run_id, observed_at_ms, locator, record_json) VALUES ('docket-rev:fixture', 'global', 'w', 'v', "
                 "'courts:docket:courtlistener:99901', 's', 'courtlistener', 'nysd', 99901, '1:25-cv-09901', 'x', 1, "
                 "'r', 1, 'https://www.courtlistener.com/docket/99901/', '{}')")
    service = EnforcementLinks(conn)
    result = service.link(h.NS, scopes=h.SCOPES)
    dockets = {link["raw"]: link for link in by(result["links"], "legal.courts")}
    assert dockets["1:25-cv-09901"]["status"] == "resolved"
    assert dockets["1:25-cv-09901"]["target_revision"] == "docket-rev:fixture"
    assert dockets["3:25-cv-09903"]["status"] == "unresolved"  # the table exists now; the docket is not acquired
    assert service.link(h.NS, scopes=h.SCOPES)["created"] == 0


def test_a_reverted_match_withdraws_its_link_and_new_revisions_get_new_links(linked):
    conn, state = linked
    identity = state["identity"]
    candidate = next(c for c in identity.candidates(h.NS, scopes=h.SCOPES)
                     if c["state"] == "accepted" and c["subject_key"].startswith("enforcement:respondent:uk-fca"))
    identity.revert(h.NS, candidate["candidate_id"], "wrong entity", principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    links = EnforcementLinks(conn).link(h.NS, scopes=h.SCOPES)["links"]
    mine = [link for link in links if link["evidence"].get("candidate_id") == candidate["candidate_id"]]
    assert mine and mine[0]["status"] == "withdrawn"
    before = {link["citing_revision_id"] for link in links if link["citing_record_key"] == h.FCA_EX}
    h.apply(conn, "fca-final-notices", v2=True)
    after = EnforcementLinks(conn).link(h.NS, scopes=h.SCOPES)["links"]
    revisions = {link["citing_revision_id"] for link in after if link["citing_record_key"] == h.FCA_EX}
    assert before < revisions  # the corrected revision is linked; the earlier revision's links stay
