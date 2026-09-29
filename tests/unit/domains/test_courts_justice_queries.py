"""Dockets and decisions as of a date (#2413) and justice statistics for a place with comparability (#2416)."""

from __future__ import annotations

import pytest

from src.kb.courts_justice import CourtsJusticeError, forbidden_keys
from src.kb.courts_justice_identity import CourtsIdentity, JusticePlaces
from src.kb.justice_statistics import JusticeStatisticsStore
from src.kb.legal_court_citations import CourtCitations
from src.kb.legal_dockets import LegalDocketStore
from tests.unit import courts_justice_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    h.load_all(connection, v2=True)
    h.seed_us_code(connection)
    CourtCitations(connection).link(h.NS, scopes=h.SCOPES)
    yield connection
    connection.close()


def test_provision_to_dockets_and_decisions_as_of_a_date(conn):
    store = LegalDocketStore(conn)
    early = store.dockets_for_provision(h.NS, "42 U.S.C. § 1983", scopes=h.READ_ONLY, as_of="2099-04-01")
    assert early["status"] == "answered"
    assert [d["record_key"] for d in early["decisions"]] == [h.PRIOR_CLUSTER]  # the 2099 decision is later
    (docket,) = early["dockets"]
    assert docket["revision"]["revision_no"] == 1 and [e["entry_number"] for e in docket["citing_entries"]] == [1]
    assert docket["revision"]["retrieved_at_ms"] and docket["revision"]["source_id"] == "courtlistener-dockets"
    later = store.dockets_for_provision(h.NS, "42 USC 1983", scopes=h.READ_ONLY, as_of="2099-08-01")
    assert later["dockets"][0]["revision"]["revision_no"] == 2
    assert [e["entry_number"] for e in later["dockets"][0]["citing_entries"]] == [1, 3]
    decisions = {d["record_key"]: d for d in later["decisions"]}
    assert set(decisions) == {h.CLUSTER, h.PRIOR_CLUSTER}
    assert decisions[h.CLUSTER]["disposition"]["quoted"] == "GRANTED in part and DENIED in part"
    assert decisions[h.CLUSTER]["citing_passages"][0]["locator"]["paragraph"] == 2
    assert forbidden_keys(later) == []
    none = store.dockets_for_provision(h.NS, "42 U.S.C. § 1983", scopes=h.READ_ONLY, as_of="2098-01-01")
    assert none["status"] == "no_docket_on_record" and none["dockets"] == [] and none["decisions"] == []


def test_court_and_party_queries_use_court_ids_and_accepted_matches_only(conn):
    store = LegalDocketStore(conn)
    court = store.dockets_for_court(h.NS, "dcd", scopes=h.READ_ONLY, as_of="2099-07-15")
    assert court["dockets"][0]["date_terminated"] == "2099-06-20"
    assert court["dockets"][0]["parties"][1] == {"party_type": "natural_person", "name_as_published": None,
                                                 "pseudonym": "natural person 1 (Defendant)",
                                                 "roles": ["Defendant"], "party_key": None}
    owner = h.seed_ownership(conn)
    assert store.dockets_for_party(h.NS, owner, scopes=h.SCOPES)["status"] == "no_docket_on_record"
    identity = CourtsIdentity(conn)
    (candidate,) = identity.propose(h.NS, principal_id="a", scopes=h.REVIEW_SCOPES,
                                    ownership_namespace=h.NS)["candidates"]
    identity.review(h.NS, candidate["candidate_id"], "accept", "checked", principal_id="r", scopes=h.REVIEW_SCOPES)
    answer = store.dockets_for_party(h.NS, owner, scopes=h.SCOPES, as_of="2099-12-31")
    assert answer["status"] == "answered" and answer["connected_by"]["matches"][0]["reviewer"] == "r"
    assert answer["dockets"][0]["record_key"] == h.DOCKET and answer["decisions"][0]["record_key"] == h.CLUSTER
    for key in ("Jane Roe", "natural person 1 (Defendant)"):
        with pytest.raises(CourtsJusticeError) as caught:
            store.dockets_for_party(h.NS, key, scopes=h.SCOPES)
        assert caught.value.code == "natural_person_not_a_query_key"


def test_statistics_for_a_place_with_definitions_vintages_flags_and_gaps(conn):
    stats = JusticeStatisticsStore(conn)
    first = stats.statistics_for_place(h.NS, "us-state:EX", scopes=h.READ_ONLY, as_of="2099-10-01")
    (column,) = first["sources"]
    assert column["vintage"]["vintage_no"] == 1 and column["vintage"]["later_vintages"] == [2]
    assert column["vintage"]["retrieved_at_ms"] and column["vintage"]["source_id"] == "fbi-cde-summarized"
    clearances = next(s for s in column["series"] if "Clearances" in s["series_key"])
    march = next(o for o in clearances["observations"] if o["period"] == "2098-03")
    assert march["value"] is None and march["suppressed"] and "no value is imputed" in march["note"]
    assert clearances["definition"]["classification"] == "UCR-SRS" and clearances["definition"]["source_url"]
    assert column["coverage_notes"][0]["kind"] == "reporting_coverage"
    latest = stats.statistics_for_place(h.NS, "us-state:EX", scopes=h.READ_ONLY)
    offences = next(s for s in latest["sources"][0]["series"] if s["series_key"].endswith("actuals:Examplestate "
                                                                                          "Offenses"))
    assert [o["value"] for o in offences["observations"]] == [410, 385, 399]
    fr = stats.statistics_for_place(h.NS, "eurostat-geo:FR", scopes=h.READ_ONLY, as_of="2099-05-01")
    rate = next(s for s in fr["sources"][0]["series"] if s["series_key"].endswith(":P_HTHAB"))
    assert rate["gaps"] == [] and rate["observations"][-1]["flags"][0]["code"] == "c"
    assert {n["kind"] for n in fr["sources"][0]["coverage_notes"]} == {"national_definition_footnote",
                                                                       "esms_comparability"}
    missing = stats.statistics_for_place(h.NS, "us-state:ZZ", scopes=h.READ_ONLY)
    assert missing["status"] == "no_statistics_on_record"
    assert forbidden_keys(first) == [] and forbidden_keys(fr) == []


def test_place_ids_answer_through_resolved_mappings(conn):
    places = h.seed_places(conn)
    JusticePlaces(conn).resolve(h.NS, geo_namespace=h.NS, principal_id="a", scopes=h.SCOPES)
    answer = JusticeStatisticsStore(conn).statistics_for_place(h.NS, places["DE"], scopes=h.SCOPES)
    assert answer["status"] == "answered" and answer["place_mapping"]["mappings"][0]["code"] == "DE"
    ambiguous = JusticeStatisticsStore(conn).statistics_for_place(h.NS, places["EX-a"], scopes=h.SCOPES)
    assert ambiguous["status"] == "no_statistics_on_record"


def test_cross_jurisdiction_comparison_needs_comparability_notes(conn):
    stats = JusticeStatisticsStore(conn, now=h.Clock())
    mixed = stats.compare_places(h.NS, ["us-state:EX", "eurostat-geo:DE"], scopes=h.READ_ONLY)
    assert mixed["comparison"]["status"] == "refused_no_comparability_note"
    assert {s["place"]["code"] for s in mixed["series"]} == {"EX", "DE"}
    assert all(p["comparability"] == "comparability_unknown" for p in mixed["pairs"])
    eu = stats.compare_places(h.NS, ["eurostat-geo:DE", "eurostat-geo:FR"], scopes=h.READ_ONLY,
                              indicator="ICCS0401", as_of="2099-05-01")
    assert eu["comparison"]["status"] == "qualified_by_notes"
    assert all(p["notes"][0]["relation"] == "source_comparability_note" for p in eu["pairs"])
    assert "Comparisons between countries should be avoided" in eu["pairs"][0]["notes"][0]["statement"]
    assert forbidden_keys(eu) == []
    left = next(s["series_key"] for s in mixed["series"] if s["place"]["code"] == "EX"
                and s["series_key"].endswith("actuals:Examplestate Offenses"))
    right = next(s["series_key"] for s in mixed["series"] if s["series_key"].endswith(":ICCS0401:NR"))
    note = stats.record_note(h.NS, left, right, "not_comparable", "UCR burglary and ICCS robbery differ.",
                             [{"source_url": "https://ucr.fbi.gov/"}], principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(CourtsJusticeError):
        stats.review_note(h.NS, note["note_id"], "accept", "self", principal_id="alice", scopes=h.REVIEW_SCOPES)
    stats.review_note(h.NS, note["note_id"], "accept", "definitions cited", principal_id="bob",
                      scopes=h.REVIEW_SCOPES)
    pairs = stats.compare_places(h.NS, ["us-state:EX", "eurostat-geo:DE"], scopes=h.READ_ONLY)["pairs"]
    noted = next(p for p in pairs if {p["left"], p["right"]} == {left, right})
    assert noted["comparability"] == "not_comparable"
