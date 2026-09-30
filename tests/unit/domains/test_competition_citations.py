"""Cases, decisions and aid awards linked to cited legal acts and related cases by exact citation (#2217, CS08)."""

from __future__ import annotations

from src.kb.competition_citations import CompetitionCitations, parse_references
from tests.unit import competition_harness as h


def world(*, legal: bool = True, v2: bool = False):
    conn = h.connection()
    h.load_all(conn)
    if v2:
        h.load_all(conn, v2=True)
    works = h.seed_legal(conn) if legal else {}
    return conn, works, CompetitionCitations(conn).link(h.NS, scopes=h.SCOPES)


def test_parser_is_exact_and_never_matches_by_topic():
    raw = [c["raw"] for c in parse_references("a merger of widget makers under EU competition law")]
    assert raw == []
    found = {c["key"] for c in parse_references("Section 7 of the Clayton Act and Article 102 TFEU; see AT.40999")}
    assert found == {"usc:15:18", "tfeu:102", "case:ec:AT.40999"}


def test_legal_acts_link_by_exact_celex_through_legal_works():
    conn, works, result = world()
    links = result["links"]
    eumr = [link for link in links if link["target_key"] == "celex:32004R0139"]
    assert eumr and all(link["status"] == "resolved" and link["target_work_id"] == works["32004R0139"]
                        for link in eumr)
    gber = next(link for link in links if link["target_key"] == "celex:32014R0651")
    assert gber["status"] == "resolved" and gber["basis"] == "exact identifier 32014R0651"
    art107 = next(link for link in links if link["target_key"] == "tfeu:107")
    assert art107["target_work_id"] == works["12016E107"]
    assert art107["evidence"]["provision"] == "Article 107(3)(c) TFEU"
    # References no acquired work carries stay unresolved with their source text.
    uk = next(link for link in links if link["target_key"] == "uk:ukpga/2002/40")
    assert uk["status"] == "unresolved" and uk["raw"] == "Enterprise Act 2002"
    assert all(link["citing_revision_id"].startswith("own-rev:") for link in links)


def test_awards_link_to_sa_cases_and_unresolvable_measures_stay_unresolved():
    _, _, result = world()
    by_award = {link["citing_record_key"]: link for link in result["links"] if link["field"] == "sa_number"}
    assert by_award[h.AWARD_INT]["status"] == "resolved" and by_award[h.AWARD_INT]["target_case_key"] == h.AID_CASE
    assert by_award[h.AWARD_OTHER]["status"] == "unresolved" and by_award[h.AWARD_OTHER]["raw"] == "SA.99003"


def test_cross_authority_links_only_when_the_citation_is_explicit():
    _, _, result = world()
    cma = [link for link in result["links"] if link["citing_record_key"] == h.CMA]
    assert {(link["target_key"], link["status"]) for link in cma if link["citation_kind"] == "case"} == {
        ("case:ec:M.99001", "resolved"), ("case:uk-cma-ref:ME/9999/25", "unresolved")}
    # The FTC action concerns the same transaction but cites no EC case: no link is made.
    ftc = [link for link in result["links"] if link["citing_record_key"] == h.FTC]
    assert not [link for link in ftc if link["citation_kind"] == "case"]
    assert {link["target_key"] for link in ftc} >= {"usc:15:18", "usc:15:45"}


def test_without_a_legal_store_links_are_reported_and_resolve_later():
    conn, _, result = world(legal=False)
    assert {link["status"] for link in result["links"] if link["citation_kind"] != "case"} == {"legal_unavailable"}
    h.seed_legal(conn)
    again = CompetitionCitations(conn).link(h.NS, scopes=h.SCOPES)
    assert again["created"] == 0
    assert next(link for link in again["links"] if link["target_key"] == "celex:32004R0139")["status"] == "resolved"


def test_each_citing_revision_keeps_its_own_links():
    conn, _, result = world(v2=True)
    merger = [link for link in result["links"] if link["citing_record_key"] == h.MERGER]
    assert len({link["citing_revision_id"] for link in merger}) == 1  # only the current revision is parsed
    final = [link for link in result["links"] if link["field"] == "citation.celex"]
    assert final and final[0]["target_key"] == "celex:32025M99001" and final[0]["status"] == "unresolved"
    listed = CompetitionCitations(conn).list_links(h.NS, scopes=h.READ_ONLY)
    assert listed["unresolved"] and "not characterised" in listed["notice"]
