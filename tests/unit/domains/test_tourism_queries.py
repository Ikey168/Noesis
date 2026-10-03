"""TO07, TO08 (#2739): a tourism indicator for a place as of a release, and a series' history with comparability."""

from __future__ import annotations

import pytest

from src.kb.tourism_identity import TourismIdentity
from src.kb.tourism_queries import TourismQueries
from src.kb.tourism_records import TourismError, forbidden_paths, personal_data_paths
from tests.unit import tourism_harness as h

GERMANY = {"scheme": "eurostat-geo", "code": "DE", "nuts_version": "2021"}
BERLIN = {"scheme": "eurostat-geo", "code": "DE30", "nuts_version": "2021"}


@pytest.fixture(scope="module")
def conn():
    conn = h.connection()
    h.load_all(conn, revisions=True, nuts2024=True)
    return conn


def test_as_of_answers_select_the_vintage_released_by_the_date_and_cite_it(conn):
    queries = TourismQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                        residence="TOTAL", period="2096-04", as_of="2024-04-30")
    late = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                       residence="TOTAL", period="2096-04", as_of="2024-05-31")
    (before,), (after,) = early["results"], late["results"]
    assert before["values"] == [{**before["values"][0], "period": "2096-04", "value": "30112600",
                                 "flags": {"OBS_FLAG": "p"}}]
    assert after["values"][0]["value"] == "30245100" and after["values"][0]["flags"] == {}
    assert after["vintage"]["revision_of"] == before["vintage"]["vintage_id"]
    for row in (before, after):
        assert row["citation"]["vintage_id"] == row["vintage"]["vintage_id"]
        assert row["citation"]["as_of"] == row["vintage"]["release_at"]
        assert row["definition"]["content"]["source_text"].startswith("Nights spent")
        assert row["coverage_threshold"].startswith("establishments with 10 or more bed places")
    nothing = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, as_of="2024-01-01")
    assert nothing["status"] == "none_published" and len(nothing["unavailable_by_as_of"]) == 6


def test_monthly_and_annual_series_are_never_combined_and_confidential_cells_are_a_status(conn):
    queries = TourismQueries(conn)
    berlin = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=BERLIN, concept="nights_spent")
    (row,) = berlin["results"]
    assert row["frequency"] == "annual" and berlin["by_frequency"] == {"annual": [row["series_id"]]}
    cells = {v["period"]: v for v in row["values"]}
    assert cells["2095"]["status"] == "confidential" and cells["2095"]["value"] is None
    assert row["gaps"]["confidential"] == [{"period": "2095", "status": "confidential", "flags": {"OBS_FLAG": "c"}}]
    # Germany: monthly series answer a monthly period; an annual period is never computed from months.
    annual_request = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                                 period="2096")
    assert annual_request["results"] == [] and len(annual_request["other_frequency"]) == 3
    assert all("never computed" in o["reason"] for o in annual_request["other_frequency"])
    both = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "eurostat-geo", "code": "DE"})
    assert set(both["by_frequency"]) == {"monthly"}
    assert all(p["combined"] is False for p in both["comparability"])
    assert "never combined" in both["never_combined"]
    for answer in (berlin, annual_request, both):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []


def test_un_tourism_reports_not_implemented_never_empty(conn):
    queries = TourismQueries(conn)
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, providers=["un-tourism"])
    assert answer["status"] == "not-implemented" and answer["results"] == []
    (entry,) = answer["not_implemented"]
    assert entry["provider"] == "un-tourism" and "not an empty answer" in entry["reason"]
    default = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY)
    assert default["status"] == "reported" and default["not_implemented"][0]["status"] == "not-implemented"
    with pytest.raises(TourismError):
        queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, providers=["unwto-scrape"])


def test_place_ids_and_other_nuts_versions_answer_through_accepted_identity_only(conn):
    queries = TourismQueries(conn)
    places = h.register_places(conn, keys=("berlin",))
    assert queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["berlin"])["unmatched_place"] is True
    identity = TourismIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "code", principal_id="reviewer",
                            scopes=h.SCOPES)
    by_place = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["berlin"], as_of="2024-07-31")
    assert {r["dataset"] for r in by_place["results"]} == {"tour_occ_nin2", "tour_cap_nuts2"}
    assert {r["area_basis"] for r in by_place["results"]} == {"accepted-match"}
    # The NUTS 2024 key reaches NUTS 2021 rows only through an accepted correspondence link, labelled as such.
    key_2024 = {**BERLIN, "nuts_version": "2024"}
    alone = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=key_2024, concept="bed_places")
    assert {r["area"]["nuts_version"] for r in alone["results"]} == {"2024"}
    identity.import_correspondence(h.NS, h.correspondence_tables()[0], principal_id="op", scopes=h.SCOPES)
    for link in identity.propose_correspondence_links(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]:
        identity.review(h.NS, link["assertion_id"], "accept", "Eurostat correspondence", principal_id="reviewer",
                        scopes=h.SCOPES)
    linked = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=key_2024, concept="bed_places",
                                         as_of="2024-07-31")
    rows = {r["area"]["nuts_version"]: r for r in linked["results"]}
    assert set(rows) == {"2021"}  # the 2024 series was released later; the 2021 row comes through the link
    assert rows["2021"]["area_basis"] == "accepted NUTS correspondence (unchanged)"
    unversioned = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "eurostat-geo",
                                                                                "code": "DE30"},
                                              concept="bed_places")
    assert {r["area"]["nuts_version"] for r in unversioned["results"]} == {"2021", "2024"}
    pair = next(p for p in unversioned["comparability"])
    assert {d["kind"] for d in pair["recorded_differences"]} == {"different_nuts_version"}


def test_history_lists_revisions_with_comparability_notes_and_cites_each_vintage(conn):
    queries = TourismQueries(conn)
    nights = h.series_by(conn, dataset="tour_occ_nim", residence="FOR")
    history = queries.series_history(h.NS, nights["series_id"], scopes=h.READ_ONLY)
    first, second = history["vintages"]
    assert first["citation"]["vintage_id"] == first["vintage_id"] and second["revision_of"] == first["vintage_id"]
    assert second["changed_periods"] == ["2096-04"] and second["new_periods"] == ["2096-05"]
    (pair,) = history["pairs"]
    kinds = {n["kind"] for n in pair["notes"]}
    assert "provisional_revised" in kinds and pair["comparability"] == "noted"
    assert [c["vintage_id"] for c in pair["citations"]] == [first["vintage_id"], second["vintage_id"]]
    # Annual NUTS 2 nights: the provisional 2096 value revised; the d flag (definition differs) is a series note.
    regional = h.series_by(conn, dataset="tour_occ_nin2")
    regional_history = queries.series_history(h.NS, regional["series_id"], scopes=h.READ_ONLY)
    (regional_pair,) = regional_history["pairs"]
    assert {n["kind"] for n in regional_pair["notes"]} >= {"provisional_revised"}
    assert any(n["relation"] == "definition_differs" for n in regional_history["series_notes"])
    assert regional_history["vintages"][0]["coverage_threshold"].startswith("establishments with 10")
    # A pair with no source-stated note is comparability_unknown.
    arrivals = h.series_by(conn, dataset="tour_occ_arm", residence="TOTAL")
    arrivals_pair = queries.series_history(h.NS, arrivals["series_id"], scopes=h.READ_ONLY)["pairs"][0]
    assert arrivals_pair["comparability"] == "noted"  # provisional April revised
    identity = TourismIdentity(conn)  # the correspondence import and proposal are idempotent
    identity.import_correspondence(h.NS, h.correspondence_tables()[0], principal_id="op", scopes=h.SCOPES)
    identity.propose_correspondence_links(h.NS, principal_id="analyst", scopes=h.SCOPES)
    beds = h.series_by(conn, indicator="BEDPL", nuts_version="2021")
    beds_history = queries.series_history(h.NS, beds["series_id"], scopes=h.READ_ONLY)
    removal_pair = beds_history["pairs"][-1]
    assert {n["kind"] for n in removal_pair["notes"]} >= {"removed_by_source", "nuts_version_change"}
    assert any(link["kind"] == "nuts_version_change" for link in beds_history["nuts_version_links"])


def test_a_pair_without_notes_is_comparability_unknown():
    conn = h.connection()
    h.apply(conn, "capacity", retrieved_at_ms=h.FIRST_RETRIEVAL)
    h.apply(conn, "capacity", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    estbl = h.series_by(conn, indicator="ESTBL")
    history = TourismQueries(conn).series_history(h.NS, estbl["series_id"], scopes=h.READ_ONLY)
    (pair,) = history["pairs"]
    assert pair["comparability"] == "noted"  # 2096 provisional revised
    from src.kb.tourism_queries import TourismQueries as Q

    bare = Q._history_pair({"vintage_id": "a", "release_at": "x", "coverage_threshold": None, "citation": {}},
                           {"vintage_id": "b", "release_at": "y", "definition_change": None, "removed_by_source": None,
                            "revisions": [], "changed_periods": [], "new_periods": ["2097"],
                            "coverage_threshold": None, "citation": {}}, [], [])
    assert bare["comparability"] == "comparability_unknown"


def test_the_evidence_bundle_cites_every_item_with_source_revision_and_as_of(conn):
    queries = TourismQueries(conn)
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=BERLIN, as_of="2024-07-31")
    bundle = queries.evidence_bundle(answer)
    assertions = bundle["sections"][0]["assertions"]
    assert len(assertions) == sum(len(r["values"]) for r in answer["results"])
    for assertion in assertions:
        (dependency,) = assertion["dependencies"]
        assert dependency["revision"].startswith("to-vintage:") and dependency["as_of"]
        assert assertion["citations"] and assertion["source"].startswith("eurostat-tourism-")
    confidential = next(a for a in assertions if a["id"].endswith(":2095") and "[confidential]" in a["text"])
    assert "OBS_FLAG c" in confidential["text"]
    assert bundle["not_implemented"][0]["provider"] == "un-tourism"
    assert {b["id"] for b in bundle["bibliography"]} == {r["citation"]["release_id"] for r in answer["results"]}
