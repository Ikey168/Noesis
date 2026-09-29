"""Comparability across publishers and definition revisions without merging series (#1969)."""

from __future__ import annotations

import builtins

import pytest

from src.kb.demographics import DemographicError, DemographicStore, forbidden_keys
from src.kb.demographics_comparability import DemographicComparability
from tests.unit import demographics_harness as h


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_all(conn)
    h.load_bamf(conn)
    yield conn
    conn.close()


def _series(conn, **filters):
    return DemographicStore(conn, initialize=False).find_series(h.NS, **filters)


def test_publishers_are_side_by_side_without_a_merged_value_and_unknown_pairs_say_so(
    conn,
):
    comparability = DemographicComparability(conn)
    answer = comparability.side_by_side(
        h.NS,
        "asylum_applications",
        scopes=h.READ_ONLY,
        geography_codes=["DE"],
        period_from="2098",
    )
    assert {c["provider"] for c in answer["series"]} == {"eurostat", "bamf"}
    for column in answer["series"]:
        assert column["definition"]["content"]["procedure"] == "application"
        assert [o["period"] for o in column["observations"]] == ["2098"]
    (pair,) = answer["pairs"]
    assert pair["comparability"] == "comparability_unknown" and pair["notes"] == []
    assert forbidden_keys(answer) == []
    assert not any(k in answer for k in ("total", "merged", "average"))


def test_notes_are_cited_reviewable_and_reversible(conn):
    comparability = DemographicComparability(conn)
    eurostat = _series(conn, provider="eurostat", concept="asylum_applications")[0]
    bamf = next(
        s
        for s in _series(conn, provider="bamf")
        if s["indicator"] == "applications_first"
    )
    note = comparability.record(
        h.NS,
        {"series_id": eurostat["series_id"]},
        {"series_id": bamf["series_id"]},
        "same_concept_different_definition",
        "Eurostat counts first-time applicants; the BAMF report counts Erstanträge (fictional note)",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert note["state"] == "proposed"
    assert {c["provider"] for c in note["cited"]} == {"eurostat", "bamf"}
    assert all(
        c["source_revision"]["release_id"] and c["definition_id"] for c in note["cited"]
    )
    again = comparability.record(
        h.NS,
        {"series_id": bamf["series_id"]},
        {"series_id": eurostat["series_id"]},
        "same_concept_different_definition",
        "Eurostat counts first-time applicants; the BAMF report counts Erstanträge (fictional note)",
        principal_id="bob",
        scopes=h.SCOPES,
    )
    assert again["note_id"] == note["note_id"]
    with pytest.raises(DemographicError):
        comparability.review(
            h.NS, note["note_id"], "accept", "ok", principal_id="alice", scopes=h.SCOPES
        )
    accepted = comparability.review(
        h.NS,
        note["note_id"],
        "accept",
        "checked",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert accepted["state"] == "accepted"
    answer = comparability.side_by_side(
        h.NS, "asylum_applications", scopes=h.READ_ONLY, geography_codes=["DE"]
    )
    assert answer["pairs"][0]["comparability"] == "same_concept_different_definition"
    reverted = comparability.revert(
        h.NS, note["note_id"], "wrong", principal_id="rev", scopes=h.REVIEW_SCOPES
    )
    assert reverted["state"] == "reverted"
    answer = comparability.side_by_side(
        h.NS, "asylum_applications", scopes=h.READ_ONLY, geography_codes=["DE"]
    )
    assert answer["pairs"][0]["comparability"] == "comparability_unknown"
    with pytest.raises(DemographicError):
        comparability.revert(
            h.NS, note["note_id"], "again", principal_id="rev", scopes=h.REVIEW_SCOPES
        )


def test_definition_revisions_of_one_series_are_a_pair_and_breaks_stay_marked(conn):
    comparability = DemographicComparability(conn)
    answer = comparability.side_by_side(
        h.NS, "population_stock", scopes=h.READ_ONLY, geography_codes=["001"]
    )
    residents = next(c for c in answer["series"] if c["indicator"] == "residents")
    (base_break,) = residents["breaks"]
    assert base_break["kind"] == "census_base_change"
    assert "no chaining" in base_break["handling"]
    definition_pairs = [p for p in answer["pairs"] if p["left"]["kind"] == "definition"]
    assert definition_pairs and all(
        p["comparability"] == "comparability_unknown" for p in definition_pairs
    )
    note = comparability.record(
        h.NS,
        {"definition_id": base_break["from_definition_id"]},
        {"definition_id": base_break["to_definition_id"]},
        "same_concept_different_definition",
        "Fortschreibung on the 2011 census base versus the 2022 census base",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    answer = comparability.side_by_side(
        h.NS, "population_stock", scopes=h.READ_ONLY, geography_codes=["001"]
    )
    assert any(
        p["notes"] and p["notes"][0]["note_id"] == note["note_id"]
        for p in answer["pairs"]
    )


def test_unit_conversion_uses_a_receipt_and_never_stores_or_crosses_count_and_rate(
    conn, monkeypatch
):
    comparability = DemographicComparability(conn)
    series = _series(conn, provider="eurostat", series_code="demo_pjan")[0]
    before = conn.execute("SELECT count(*) FROM demographic_series").fetchone()[0]
    converted = comparability.convert(
        h.NS, series["series_id"], "thousand persons", scopes=h.READ_ONLY
    )
    assert converted["observations"][0]["converted"]["value"] == "90000.100"
    assert converted["stored"] is False and converted["calculation_receipt"]["digest"]
    assert (
        conn.execute("SELECT count(*) FROM demographic_series").fetchone()[0] == before
    )
    with pytest.raises(DemographicError) as refused:
        comparability.convert(
            h.NS, series["series_id"], "per 1000 persons", scopes=h.READ_ONLY
        )
    assert refused.value.code == "incompatible_units"
    real_import = builtins.__import__

    def no_pint(name, *args, **kwargs):
        if name == "pint" or name.startswith("pint."):
            raise ModuleNotFoundError("No module named 'pint'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pint)
    fallback = comparability.convert(
        h.NS, series["series_id"], "thousand persons", scopes=h.READ_ONLY
    )
    assert fallback["observations"][0]["converted"] == {
        "value": "90000.100",
        "unit": "thousand persons",
        "method": "exact decimal scale (pint not installed)",
    }


def test_scopes_are_checked(conn):
    comparability = DemographicComparability(conn)
    with pytest.raises(DemographicError):
        comparability.side_by_side(
            h.NS, "population_stock", scopes={f"namespace:{h.NS}:read"}
        )
    series = _series(conn, provider="eurostat")
    with pytest.raises(DemographicError):
        comparability.record(
            h.NS,
            {"series_id": series[0]["series_id"]},
            {"series_id": series[1]["series_id"]},
            "not_comparable",
            "x",
            principal_id="p",
            scopes=h.READ_ONLY,
        )
