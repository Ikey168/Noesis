"""Institution-statistic, education-indicator and comparability-note records with release vintages (#2378)."""

from __future__ import annotations

import json

import pytest

from src.kb.education_statistics import (
    EducationError,
    EducationStatisticsStore,
    forbidden_keys,
    readiness,
)
from tests.unit import education_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def series_for(store, code, subject):
    return next(s for s in store.find_series(h.NS, indicator=code) if s["subject"]["code"] == subject)


def test_the_contract_declares_every_record_type_and_forbids_rankings_and_scores():
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-education-statistic-record-v1.json").read_text())
    assert schema["$id"] == "noesis-education-statistic-record-v1"
    assert set(schema["properties"]["record_type"]["enum"]) == {
        "release", "institution_profile", "series", "vintage", "observation", "comparability_note"}
    assert {"rank"} in [set(r["required"]) for r in schema["not"]["anyOf"]]
    assert "suppressed" in schema["definitions"]["observation"]["properties"]["status"]["enum"]


def test_institution_statistics_carry_source_id_indicator_unit_period_vintage_and_receipt(conn):
    store = EducationStatisticsStore(conn, initialize=False)
    series = series_for(store, "EFTOTLT", "100001")
    assert series["record_kind"] == "institution_statistic"
    assert series["subject"] == {"scheme": "ipeds-unitid", "code": "100001", "country": "US"}
    answer = store.values(h.NS, series["series_id"])
    revision = answer["vintage"]["source_revision"]
    assert revision["release_stage"] == "provisional" and revision["published_on"] == "2099-01-15"
    assert revision["evidence_origin"] == "fixture" and len(revision["file_sha256"]) == 64
    assert revision["attribution"].startswith("U.S. Department of Education")
    assert answer["observations"][0]["value"] == "25000"
    assert answer["vintage"]["definition"]["label"] == "Grand total"
    profiles = store.profiles(h.NS, "ipeds-unitid", "100001")
    assert profiles[0]["subject"]["label"] == "Fictional State University"


def test_education_indicators_carry_isced_level_definition_and_comparability_notes(conn):
    store = EducationStatisticsStore(conn, initialize=False)
    series = series_for(store, "GER.5T8", "FRA")
    assert series["record_kind"] == "education_indicator" and series["isced"]["level"] == "5T8"
    answer = store.values(h.NS, series["series_id"])
    (note,) = answer["comparability_notes"]
    assert (note["kind"], note["code"], note["source"]) == ("qualifier", "UIS_EST", "publisher")
    assert answer["vintage"]["definition"]["definition"].startswith("Total enrolment in tertiary education")


def test_a_revised_value_adds_a_revision_and_earlier_vintages_stay_queryable(conn):
    store = EducationStatisticsStore(conn, initialize=False)
    for name in h.REVISIONS:
        h.apply(conn, name, revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    series = series_for(store, "EFTOTLT", "100001")
    first, final = store.vintage_rows(h.NS, series["series_id"])
    assert (first["release_stage"], final["release_stage"]) == ("provisional", "final")
    assert final["revision_of"] == first["vintage_id"] and final["values_changed"]
    assert store.observations(h.NS, first["vintage_id"])[0]["value"] == "25000"
    assert store.observations(h.NS, final["vintage_id"])[0]["value"] == "25140"
    before = store.values(h.NS, series["series_id"], as_of=h.day_ms("2099-06-01"))
    assert before["vintage"]["vintage_id"] == first["vintage_id"]
    unreleased = store.values(h.NS, series["series_id"], as_of=h.day_ms("2098-12-31"))
    assert unreleased["status"] == "unavailable" and unreleased["reason"] == "no_release_by_as_of"
    # An unchanged series in a later release is a new vintage without changed values.
    gerd_de = series_for(store, "GERD_HES", "DE")
    assert [v["values_changed"] for v in store.vintage_rows(h.NS, gerd_de["series_id"])] == [True, False]
    gerd_fr = series_for(store, "GERD_HES", "FR")
    assert store.values(h.NS, gerd_fr["series_id"])["observations"][-1]["value"] == "12850.1"


def test_reacquisition_is_idempotent_and_changed_values_without_a_new_release_clock_are_refused(conn):
    store = EducationStatisticsStore(conn, initialize=False)
    counts = conn.execute("SELECT count(*) FROM edu_vintages").fetchone()[0]
    again = h.apply(conn, "eter", retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert {r["status"] for r in again} == {"unchanged"}
    assert conn.execute("SELECT count(*) FROM edu_vintages").fetchone()[0] == counts
    header = dict(h.fetch("eter")[0][0]["education_release"])
    item = json.loads(json.dumps(next(r["education_item"] for r in h.fetch("eter")[0]
                                      if r["education_item"]["record_kind"] == "institution_statistic")))
    item["observations"][0]["value"] = item["observations"][0]["value_text"] = "1"
    item["observations"][0]["status"] = "reported"
    header["file_sha256"] = "f" * 64
    header["item_count"] = 1
    with pytest.raises(EducationError) as caught:
        store.apply_release(h.NS, header, [item], run_id="tamper", source_id="eter-institution-statistics")
    assert caught.value.code == "vintage_conflict"


def test_records_never_carry_rankings_scores_or_numbers_for_values_that_are_not_reported(conn):
    store = EducationStatisticsStore(conn, initialize=False)
    header = dict(h.fetch("oecd")[0][0]["education_release"])
    item = json.loads(json.dumps(h.fetch("oecd")[0][0]["education_item"]))
    header["item_count"] = 1
    with pytest.raises(EducationError):
        store.apply_release(h.NS, {**header, "file_sha256": "a" * 64}, [{**item, "rank": 1}], run_id="x",
                            source_id="s")
    missing = dict(item, observations=[{"period": "2096", "value": "0", "status": "missing"}])
    with pytest.raises(EducationError):
        store.apply_release(h.NS, {**header, "file_sha256": "b" * 64}, [missing], run_id="x", source_id="s")
    assert forbidden_keys({"a": [{"quality_score": 1}]}) == ["$.a[0].quality_score"]
    stored = conn.execute("SELECT count(*) FROM edu_observations WHERE status<>'reported' AND value IS NOT NULL")
    assert stored.fetchone()[0] == 0


def test_readiness_reports_each_provider_decision_and_the_feature_is_off_without_a_plan(conn):
    report = readiness(conn)
    assert report["selected"] is False and report["stores_ready"] is True
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    assert report["providers"]["ipeds"]["releases"] == 5
    assert readiness(h.connection())["stores_ready"] is False
