"""Labour indicator, definition, observation and release-vintage records in the Economics series storage (#2453)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.labour_statistics import (
    CONTRACT,
    LabourComparability,
    LabourError,
    LabourStore,
    forbidden_keys,
    register_schemas,
)
from tests.unit import labour_harness as h

SCHEMA = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    return conn


def test_values_live_in_the_existing_economic_series_storage(loaded):
    series = h.series_by_key(loaded, "ilostat", "DEU.A.UNE_DEAP_RT.SEX_T.AGE_YTHADULT_YGE15")
    indicator = loaded.execute(
        "SELECT seasonal_adjustment, attributes_json FROM economic_indicators WHERE indicator_id=?",
        [series["economic_series"]["indicator_id"]],
    ).fetchone()
    assert indicator[0] == "not_adjusted" and json.loads(indicator[1])["estimate_type"] == "national-reported"
    vintages = loaded.execute(
        "SELECT as_of, revision_of FROM economic_vintages WHERE series_id=? ORDER BY as_of", [series["series_id"]]
    ).fetchall()
    assert len(vintages) == 2 and vintages[1][1] == vintages[0][0]
    stored = loaded.execute(
        "SELECT as_of, value FROM dataset_observations WHERE series_id=? AND period='2023' ORDER BY as_of",
        [series["series_id"]],
    ).fetchall()
    assert [v for _, v in stored] == [3.1, 3.0]
    # No labour-specific value table duplicates the numeric store: numbers are read back from it.
    first, second = LabourStore(loaded).vintage_rows(h.NS, series["series_id"])
    old = {o["period"]: o for o in LabourStore(loaded).observations(h.NS, first["vintage_id"])}
    assert old["2023"]["value"] == "3.1" and old["2023"]["numeric_value"] == 3.1
    assert second["revision_of"] == first["vintage_id"] and second["changes"]["revised"][0]["period"] == "2023"


def test_indicator_records_carry_every_declared_dimension(loaded):
    store = LabourStore(loaded)
    for series in store.find_series(h.NS):
        assert series["seasonal_adjustment"] in {"NSA", "SA", "trend"}
        assert series["definition_basis"] in {"ilo-harmonised", "oecd-harmonised", "eu-lfs", "national"}
        assert series["native_key"] and series["dataflow"]["reference"] and series["unit"]["label"]
        assert series["references"], series["native_key"]
    modelled = store.find_series(h.NS, estimate_type="ilo-modelled")
    national = store.find_series(h.NS, provider="ilostat", concept="unemployment_rate",
                                 estimate_type="national-reported")
    assert {s["area"]["code"] for s in modelled} == {s["area"]["code"] for s in national} == {"DEU", "USA"}
    assert not {s["series_id"] for s in modelled} & {s["series_id"] for s in national}


def test_prior_vintages_stay_queryable_and_as_of_selects_by_release_clock(loaded):
    store = LabourStore(loaded)
    series = h.series_by_key(loaded, "eurostat-lfs", "A.PC.T.Y15-74.TOTAL.DE")
    march = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-04-01"))
    april = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-05-01"))
    assert {o["period"]: o["value"] for o in march["observations"]} == {"2022": "3.2", "2023": "3.0"}
    assert {o["period"]: o["value"] for o in april["observations"]} == {"2022": "3.2", "2023": "3.1", "2024": "2.9"}
    assert store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-01-01"))["reason"] == \
        "no_release_by_as_of"
    changes = april["vintage"]["changes"]
    assert changes["new_periods"] == ["2024"] and [r["period"] for r in changes["revised"]] == ["2023"]


def test_re_acquisition_is_idempotent_and_conflicting_values_are_refused(loaded):
    before = len(LabourStore(loaded).releases(h.NS))
    assert {r["status"] for r in h.apply(loaded, "oecd", retrieved_at_ms=h.SECOND_RETRIEVAL)} == {"unchanged"}
    assert len(LabourStore(loaded).releases(h.NS)) == before
    # A release that changes values without a new release clock is refused; the stored vintage is kept.
    records = h.fetch("eurostat", revision=True)[0]
    header = dict(records[0]["labour_release"], file_sha256="0" * 64)
    item = json.loads(json.dumps(records[0]["labour_item"]))
    item["observations"][0]["value"] = item["observations"][0]["value_text"] = "9.9"
    with pytest.raises(LabourError) as caught:
        LabourStore(loaded).apply_release(h.NS, header, [item], run_id="x", source_id="eurostat-lfs-labour",
                                          retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert caught.value.code == "vintage_conflict"
    item["observations"][0]["nowcast"] = "3.0"
    with pytest.raises(LabourError):
        LabourStore(loaded).apply_release(h.NS, header, [item], run_id="x", source_id="s")


def test_source_notes_become_definition_and_comparability_notes(loaded):
    store = LabourStore(loaded)
    series = h.series_by_key(loaded, "ilostat", "DEU.A.UNE_DEAP_RT.SEX_T.AGE_YTHADULT_YGE15")
    definition = store.definition(h.NS, series["current_definition_id"])
    assert {"kind": "source-attribute", "attribute": "SOURCE", "value": "BA:1234"} in definition["content"][
        "source_notes"]
    notes = LabourComparability(loaded).notes(h.NS, scopes=h.READ_ONLY, series_id=series["series_id"])
    breaks = [n for n in notes if n["relation"] == "break_in_series"]
    assert breaks and breaks[0]["periods"] == ["2023"] and breaks[0]["state"] == "source-stated"


def test_comparability_notes_follow_the_reviewable_structure(loaded):
    comparability = LabourComparability(loaded)
    left = h.series_by_key(loaded, "ilostat", "DEU.A.UNE_DEAP_RT.SEX_T.AGE_YTHADULT_YGE15")
    right = h.series_by_key(loaded, "ilostat", "DEU.A.UNE_2EAP_RT.SEX_T.AGE_YTHADULT_YGE15")
    note = comparability.record(h.NS, {"series_id": left["series_id"]}, {"series_id": right["series_id"]},
                                "different_definition_basis", "national LFS definitions against ILO modelled "
                                "estimates", principal_id="analyst", scopes=h.SCOPES)
    assert note["state"] == "proposed" and {c["definition"]["basis"] for c in note["cited"]} == {
        "national", "ilo-harmonised"}
    accepted = comparability.review(h.NS, note["note_id"], "accept", "stated by both sources",
                                    principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted"
    assert comparability.revert(h.NS, note["note_id"], "reconsidered", principal_id="reviewer",
                                scopes=h.SCOPES)["state"] == "reverted"
    with pytest.raises(LabourError):
        comparability.record(h.NS, {"series_id": left["series_id"]}, None, "different_definition_basis", "x",
                             principal_id="a", scopes=h.SCOPES)
    with pytest.raises(LabourError):
        comparability.record(h.NS, {"series_id": left["series_id"]}, {"series_id": right["series_id"]},
                             "not_comparable", "x", principal_id="a", scopes=h.READ_ONLY)


def test_records_round_trip_through_the_registered_schema(loaded):
    validator = Draft7Validator(SCHEMA)
    store = LabourStore(loaded)
    comparability = LabourComparability(loaded)
    for series in store.find_series(h.NS):
        assert list(validator.iter_errors(series)) == [], series["native_key"]
        assert forbidden_keys(series) == []
        for vintage in store.vintage_rows(h.NS, series["series_id"]):
            full = store.vintage(h.NS, vintage["vintage_id"])
            assert list(validator.iter_errors(full)) == []
            for obs in store.observations(h.NS, vintage["vintage_id"]):
                assert list(validator.iter_errors({"contract": CONTRACT, **obs})) == []
        definition = store.definition(h.NS, series["current_definition_id"])
        assert list(validator.iter_errors(definition)) == []
    for release in store.releases(h.NS):
        assert list(validator.iter_errors(release)) == []
    for note in comparability.notes(h.NS, scopes=h.READ_ONLY):
        assert list(validator.iter_errors(note)) == []
    registered = register_schemas(loaded, principal_id="svc", scopes={"operator", "knowledge:schema:register",
                                                                      "namespace:global:write"})
    assert registered and registered[0]
