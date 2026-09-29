"""Docket entries and opinions linked to cited provisions and decisions by exact citation (#2410)."""

from __future__ import annotations

import pytest

from src.kb.legal import LegalStore
from src.kb.legal_court_citations import CourtCitations, parse_us_citations
from tests.unit import courts_justice_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    h.seed_us_code(connection)
    yield connection
    connection.close()


def test_parser_reads_statutes_regulations_and_reporters_exactly():
    parsed = parse_us_citations("See 42 U.S.C. § 1983; 15 U.S.C. §45(a); 5 C.F.R. § 2635.101; 990 F.4th 12 and "
                                "999 F. Supp. 4th 201. The 1983 season is not a citation.")
    assert [(c["kind"], c["key"]) for c in parsed] == [
        ("statute", "usc:42:1983"), ("statute", "usc:15:45"), ("regulation", "cfr:5:2635.101"),
        ("case", "reporter:990f.4th12"), ("case", "reporter:999f.supp.4th201")]
    assert parsed[1]["provision"] == "§ 45(a)" and parsed[0]["normalized"] == "42 U.S.C. § 1983"
    assert parse_us_citations("a topic about civil rights") == []


def test_links_record_the_citing_revision_locator_and_keep_unresolved_citations(conn):
    result = CourtCitations(conn).link(h.NS, scopes=h.SCOPES)
    links = result["links"]
    statute = [link for link in links if link["target_key"] == "usc:42:1983"]
    assert {link["citing_kind"] for link in statute} == {"docket-entry", "opinion"}
    assert all(link["status"] == "resolved" and link["target_work_id"] == "legal-work:fixture-usc-title-42"
               and link["target_provision"] == "§ 1983" for link in statute)
    entry = next(link for link in statute if link["citing_kind"] == "docket-entry")
    assert entry["locator"]["entry_number"] == 1 and entry["citing_revision_id"].startswith("legal-docket-revision:")
    opinion = next(link for link in statute if link["citing_record_key"] == h.CLUSTER)
    assert opinion["locator"]["opinion_id"] == 90001 and opinion["locator"]["paragraph"] == 2
    case = next(link for link in links if link["target_key"] == "reporter:990f.4th12"
                and link["citing_record_key"] == h.CLUSTER)
    prior = LegalStore(conn).lookup(h.NS, scopes=h.READ_ONLY, identifier="990 F.4th 12")["works"][0]["work_id"]
    assert case["status"] == "resolved" and case["target_work_id"] == prior
    by_id = next(link for link in links if link["target_key"] == "courtlistener-opinion:90002")
    assert by_id["target_work_id"] == prior
    unresolved = {link["normalized"] for link in links if link["status"] == "unresolved"}
    assert {"15 U.S.C. § 45(a)", "5 C.F.R. § 2635.101", "123 F.3d 456"} <= unresolved
    assert all(link["raw"] for link in links)
    again = CourtCitations(conn).link(h.NS, scopes=h.SCOPES)
    assert again["created"] == 0 and len(again["links"]) == len(links)


def test_citations_appear_on_the_legal_work_and_new_revisions_get_their_own_links(conn):
    CourtCitations(conn).link(h.NS, scopes=h.SCOPES)
    usc = LegalStore(conn).inspect(h.NS, "legal-work:fixture-usc-title-42", scopes=h.READ_ONLY)
    assert usc["cited_by"]
    h.apply(conn, "courtlistener-dockets", v2=True)
    CourtCitations(conn).link(h.NS, scopes=h.SCOPES)
    entries = [link for link in CourtCitations(conn).links(h.NS, citing_record_key=h.DOCKET)
               if link["target_key"] == "usc:42:1983"]
    assert len({link["citing_revision_id"] for link in entries}) == 2
    assert {link["locator"]["entry_number"] for link in entries} == {1, 3}
