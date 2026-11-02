"""#2882: wealth figures for a place as of a release, side by side and never combined, and a series' history."""

from __future__ import annotations

import pytest

from src.kb.wealth_distribution_queries import (
    WealthQueries,
    export_profile,
    place_profile,
)
from src.kb.wealth_distribution_records import (
    WealthError,
    forbidden_paths,
    personal_data_paths,
)
from src.kb.wealth_distribution_store import WealthStore
from tests.unit import wealth_distribution_harness as h


@pytest.fixture(scope="module")
def conn():
    connection = h.connection()
    h.load_all(connection, revisions=True)
    return connection


def cell(answer, native_key, period):
    row = next(r for r in answer["results"] if r["native_key"] == native_key)
    return row, {o["period"]: o for o in row["observations"]}.get(period)


def test_as_of_selects_the_vintage_released_by_that_date(conn):
    queries = WealthQueries(conn)
    before = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2098-06-01")
    assert before["results"] == [] and {u["reason"] for u in before["unavailable_by_as_of"]} == {
        "no_release_by_as_of"}
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2098-12-31")
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2099-06-30")
    row, extrapolated = cell(early, "DE.shweal_p99p100_992_j", "2097")
    assert row["vintage"]["release_label"].startswith("WID update 2098-06")
    assert extrapolated["publication_status"] == "extrapolated"
    row, replaced = cell(later, "DE.shweal_p99p100_992_j", "2097")
    assert row["vintage"]["release_label"].startswith("WID update 2099-03")
    assert replaced["publication_status"] == "not-stated" and replaced["value_text"] != extrapolated["value_text"]
    assert row["model_based"] is True and row["definition"]["content"]["method_statement"].endswith(
        "fixture version 2 (synthetic label)")
    # Between the DWA revision (2099-01-12) and the WID revision (2099-03-15) each source has its own vintage.
    between = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2099-02-01")
    labels = {r["provider"]: r["vintage"]["release_label"] for r in between["results"]}
    assert labels["wid-wealth-distribution"].startswith("WID update 2098-06")
    assert labels["ecb-distributional-wealth-accounts"].startswith("DWA release 2099-01")


def test_sources_side_by_side_never_combined_with_comparability(conn):
    queries = WealthQueries(conn)
    germany = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, indicator="wealth_share",
                                          as_of="2099-06-30")
    assert {r["provider"] for r in germany["results"]} == {"wid-wealth-distribution",
                                                           "ecb-distributional-wealth-accounts"}
    assert germany["nothing_combined"] is True and "never combined" in germany["never_combined"]
    pairs = {tuple(sorted(p["providers"])): p["comparability"] for p in germany["pairs"]}
    assert pairs == {("ecb-distributional-wealth-accounts", "wid-wealth-distribution"): "noted"}
    noted = germany["pairs"][0]
    assert noted["notes"][0]["relation"] == "scope_difference" and noted["notes"][0]["origin"] == "source-stated"
    assert {d["kind"] for d in noted["differences"]} >= {"different_source", "different_population",
                                                         "different_method"}
    us = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.US, indicator="wealth_share",
                                     as_of="2099-06-30")
    assert {r["provider"] for r in us["results"]} == {"wid-wealth-distribution",
                                                      "fed-distributional-financial-accounts"}
    assert {p["comparability"] for p in us["pairs"]} == {"comparability_unknown"}
    assert {"provider": "ecb-distributional-wealth-accounts", "indicator": "wealth_share",
            "area": h.US} in us["none_on_record"]
    mixed = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.FR, as_of="2099-06-30")
    assert "different_indicator" in {p["comparability"] for p in mixed["pairs"]}
    removed = next(r for r in queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.FR,
                                                          indicator="wealth_median", as_of="2099-06-30")["results"])
    assert removed["status"] == "removed_by_source" and removed["observations"] == []
    for answer in (germany, us, mixed):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        for row in answer["results"]:
            assert row["citation"]["vintage_id"] and row["citation"]["as_of"]
            assert row["citation"]["live_verification"] == "unverified-live"
    groups = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.US, groups=["TopPt1", "p99.9p100"])
    assert {r["group"]["code"] for r in groups["results"]} == {"TopPt1", "p99.9p100"}


def test_a_recorded_pair_note_is_reviewed_by_another_principal(conn):
    store = WealthStore(conn)
    wid = h.series(conn, "wid-wealth-distribution", "US.shweal_p99.9p100_992_j")
    dfa = h.series(conn, "fed-distributional-financial-accounts", "TopPt1.Net worth")
    note = store.record_note(h.NS, wid["series_id"], "different_population", "WID adults vs DFA households",
                             other_series_id=dfa["series_id"], cited=[{"kind": "methodology", "url": "https://x"}],
                             principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(WealthError) as self_review:
        store.review_note(h.NS, note["note_id"], "accept", "ok", principal_id="alice", scopes=h.SCOPES)
    assert self_review.value.code == "self_review"
    answer = WealthQueries(conn).indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.US, indicator="wealth_share",
                                                     groups=["TopPt1", "p99.9p100"])
    assert answer["pairs"][0]["comparability"] == "noted"
    assert store.review_note(h.NS, note["note_id"], "reject", "no source", principal_id="bob",
                             scopes=h.SCOPES)["state"] == "rejected"
    answer = WealthQueries(conn).indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.US, indicator="wealth_share",
                                                     groups=["TopPt1", "p99.9p100"])
    assert answer["pairs"][0]["comparability"] == "comparability_unknown"


def test_series_history_shows_vintages_changes_and_comparability(conn):
    queries = WealthQueries(conn)
    wid = queries.series_history(h.NS, h.series(conn, "wid-wealth-distribution", "DE.shweal_p99p100_992_j")[
        "series_id"], scopes=h.READ_ONLY)
    assert [v["sequence"] for v in wid["vintages"]] == [1, 2]
    assert wid["vintages"][1]["new_periods"] == ["2098"]
    assert set(wid["vintages"][1]["estimates_replaced"]) == {"2096", "2097"}
    kinds = {n["kind"] for n in wid["pairs"][0]["notes"]}
    assert {"method_change", "estimate_replaced"} <= kinds
    dfa = queries.series_history(h.NS, h.series(conn, "fed-distributional-financial-accounts", "Next40.Net worth")[
        "series_id"], scopes=h.READ_ONLY)
    assert dfa["pairs"][0]["comparability"] == "comparability_unknown"
    assert dfa["vintages"][1]["changed_periods"] and all(v["citation"]["vintage_id"] for v in dfa["vintages"])
    removed = queries.series_history(h.NS, h.series(conn, "ecb-distributional-wealth-accounts",
                                                    "Q.FR.S14.N.LE.NWA._Z.EUR_MD.S.N")["series_id"],
                                     scopes=h.READ_ONLY)
    assert removed["vintages"][-1]["status"] == "removed"
    assert "removed_by_source" in {n["kind"] for n in removed["pairs"][0]["notes"]}
    unchanged = h.series(conn, "wid-wealth-distribution", "FR.wwealn_p0p100_999_i")
    assert unchanged["vintage_count"] == 2  # the revised release adds 2098 to every WID series


def test_reads_need_scope_and_the_profile_exports_a_cited_bundle(conn):
    with pytest.raises(WealthError) as denied:
        WealthQueries(conn).indicator_for_place(h.NS, scopes={"namespace:global:read"}, area=h.DE)
    assert denied.value.code == "unauthorized"
    with pytest.raises(WealthError):
        WealthQueries(conn).indicator_for_place(h.NS, scopes=h.READ_ONLY, area={"scheme": "iso3166-1-alpha3",
                                                                               "code": "DEU"})
    profile = place_profile(conn, h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2099-06-30")
    bundle = export_profile(profile)
    from src.evidence_bundle.verifier import verify_bundle

    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"] if o["payload"].get("kind") == "wealth-series-vintage"]
    assert len(cited) == len(profile["answer"]["results"]) and all(
        c["source"] and c["record_revision"] and c["as_of"] for c in cited)
