"""Demographic series, definition revisions, geography levels, vintages and breaks (#1931)."""

from __future__ import annotations

import builtins
import json

import jsonschema
import pytest

from src.kb.demographics import (
    DemographicError,
    DemographicStore,
    normalise_value,
    register_schemas,
)
from src.kb.schema_registry import SchemaRegistry
from tests.unit import demographics_harness as h

SCHEMA = json.loads(
    (
        h.ROOT / "contracts/schemas/jsonschema/noesis-demographic-series-v1.json"
    ).read_text()
)
TABLES = (
    "demographic_releases",
    "demographic_definitions",
    "demographic_geography_levels",
    "demographic_series",
    "demographic_vintages",
    "demographic_observations",
    "demographic_breaks",
    "demographic_release_members",
)


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def ids(conn):
    return sorted(
        r[0]
        for r in conn.execute("SELECT vintage_id FROM demographic_vintages").fetchall()
    )


def test_records_satisfy_the_registered_contract():
    conn = h.connection()
    h.load_all(conn)
    h.load_bamf(conn)
    store = DemographicStore(conn)
    validator = jsonschema.Draft7Validator(SCHEMA)
    for (series_id,) in conn.execute(
        "SELECT series_id FROM demographic_series"
    ).fetchall():
        series = store.series(h.NS, series_id)
        assert not list(validator.iter_errors(series))
        assert not list(validator.iter_errors(series["definition"]))
        assert not list(validator.iter_errors(series["geography_level"]))
        for vintage in store.vintage_rows(h.NS, series_id):
            view = store.vintage(h.NS, vintage["vintage_id"])
            assert not list(validator.iter_errors(view))
            for observation in store.observations(h.NS, vintage["vintage_id"]):
                assert not list(
                    validator.iter_errors({"contract": SCHEMA["$id"], **observation})
                )
        for item in series["breaks"]:
            assert not list(validator.iter_errors({"contract": SCHEMA["$id"], **item}))
    for release in store.releases(h.NS):
        assert not list(validator.iter_errors(release))
    registered = register_schemas(
        conn, principal_id="svc", scopes={"knowledge:schema:register"}
    )
    assert registered[0]["name"] == "demographic-series"
    resolved = SchemaRegistry(conn).resolve(
        "schema", "demographic-series", "^1.0.0", scopes={"knowledge:schema:read"}
    )
    assert resolved["content"]["$id"] == SCHEMA["$id"]
    assert list(
        validator.iter_errors(
            {"contract": SCHEMA["$id"], "record_type": "series", "projection": 1}
        )
    )


def test_repeated_projection_and_restart_are_idempotent():
    conn = h.connection()
    h.load_all(conn)
    h.load_bamf(conn)
    before, vintages = counts(conn), ids(conn)
    assert {r["status"] for r in h.load_all(conn)} == {"unchanged"}
    assert h.load_bamf(conn)["status"] == "unchanged"
    assert counts(conn) == before and ids(conn) == vintages


def test_a_series_cannot_be_stored_without_a_definition():
    conn = h.connection()
    page, item = h.fetch("eurostat", 0)
    record = json.loads(json.dumps(page.records[0]))
    record["demographic_series"]["definition"] = {}
    with pytest.raises(DemographicError) as refused:
        DemographicStore(conn).apply_release(
            h.NS,
            record["demographic_release"],
            [record["demographic_series"]],
            run_id="r",
            source_id="s",
        )
    assert refused.value.code == "invalid_release"
    assert counts(conn)["demographic_series"] == 0


def test_vintages_follow_the_release_clock_whatever_order_they_arrive_in():
    conn = h.connection()
    # The September update arrives before the March one.
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    h.apply(conn, "eurostat", 0)
    store = DemographicStore(conn)
    series_id = h.series_id(conn, series_code="demo_pjan")
    march, september = store.vintage_rows(h.NS, series_id)
    assert march["release_at_ms"] < september["release_at_ms"]
    assert (
        september["revision_of"] == march["vintage_id"] and march["revision_of"] is None
    )
    assert (
        store.values(h.NS, series_id)["vintage"]["vintage_id"]
        == september["vintage_id"]
    )
    # Earlier vintages stay addressable with their own values and flags.
    earlier = store.values(h.NS, series_id, vintage_id=march["vintage_id"])
    assert [o["value"] for o in earlier["observations"]] == [
        "90000100",
        "90100200",
        "90250300",
    ]
    assert earlier["observations"][-1]["flags"] == ["p"]
    # The publisher's break flag (b, September) is a marked break; values are not chained across it.
    (flag_break,) = store.breaks(h.NS, series_id)
    assert flag_break["kind"] == "publisher_flag" and flag_break["period"] == "2097"


def test_changed_values_under_an_unchanged_release_time_are_refused_and_the_vintage_kept():
    conn = h.connection()
    h.apply(conn, "eurostat", 0)
    body = h.body("eurostat_demo_pjan_de_2099-03.json").replace("90250300", "90250399")
    with pytest.raises(DemographicError) as refused:
        h.apply(conn, "eurostat", 0, body)
    assert refused.value.code == "vintage_conflict"
    series_id = h.series_id(conn, series_code="demo_pjan")
    values = DemographicStore(conn).values(h.NS, series_id)
    assert values["observations"][-1]["value"] == "90250300"
    assert counts(conn)["demographic_releases"] == 1


def test_a_reversion_to_earlier_values_is_a_new_vintage():
    conn = h.connection()
    h.apply(conn, "eurostat", 0)
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    reverted = h.body("eurostat_demo_pjan_de_2099-03.json").replace(
        "2099-03-15T23:00:00+0100", "2099-11-02T23:00:00+0100"
    )
    result = h.apply(conn, "eurostat", 0, reverted)
    assert result["vintages"] == 1
    series_id = h.series_id(conn, series_code="demo_pjan")
    rows = DemographicStore(conn).vintage_rows(h.NS, series_id)
    assert len(rows) == 3 and rows[-1]["values_changed"] is True
    assert rows[-1]["content_hash"] == rows[0]["content_hash"]


def test_a_census_base_change_is_a_new_definition_revision_and_a_marked_break():
    conn = h.connection()
    h.apply(conn, "berlin", 0)
    h.apply(conn, "berlin", 1)
    store = DemographicStore(conn)
    (mitte,) = [
        s
        for s in store.find_series(h.NS, provider="statistik-bb", geography_code="001")
        if s["indicator"] == "residents"
    ]
    history = store.definition_history(h.NS, mitte["definition_key"])
    assert [d["content"]["population_base"] for d in history] == [
        "census-2011",
        "census-2022",
    ]
    assert [d["revision_no"] for d in history] == [1, 2]
    (base_break,) = mitte["breaks"]
    assert base_break["kind"] == "census_base_change"
    assert (base_break["from_definition_id"], base_break["to_definition_id"]) == (
        history[0]["definition_id"],
        history[1]["definition_id"],
    )
    # Values never move between definition revisions.
    first, second = store.vintage_rows(h.NS, mitte["series_id"])
    assert {
        o["definition_id"] for o in store.observations(h.NS, first["vintage_id"])
    } == {history[0]["definition_id"]}
    assert {
        o["definition_id"] for o in store.observations(h.NS, second["vintage_id"])
    } == {history[1]["definition_id"]}


def test_publishers_definitions_and_levels_are_separate_series_and_levels_record_their_code_list():
    conn = h.connection()
    h.load_all(conn)
    store = DemographicStore(conn)
    populations = store.find_series(h.NS, concept="population_stock")
    assert {s["provider"] for s in populations} == {
        "eurostat",
        "destatis",
        "statistik-bb",
    }
    levels = {
        (s["geography_level"]["scheme"], s["geography_level"]["level"])
        for s in populations
    }
    assert {
        ("eu-country", "country"),
        ("nuts", "nuts1"),
        ("ags", "land"),
        ("berlin-bezirk", "bezirk"),
    } <= levels
    nuts = next(s for s in populations if s["geography_level"]["scheme"] == "nuts")
    assert nuts["geography_level"]["code_list_version"] == "NUTS 2021"
    assert len({s["series_id"] for s in populations}) == len(populations)


def test_units_normalise_through_pint_and_exactly_without_it(monkeypatch):
    assert normalise_value("12", "persons")["value"] == "12"
    assert normalise_value("1.5", "per 1000 persons")["unit"] == "per 1000 persons"
    real_import = builtins.__import__

    def no_pint(name, *args, **kwargs):
        if name == "pint" or name.startswith("pint."):
            raise ModuleNotFoundError("No module named 'pint'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pint)
    fallback = normalise_value("12.5", "thousand persons")
    assert fallback == {
        "value": "12500",
        "unit": "persons",
        "scale": "1000",
        "method": "exact decimal scale x1000 (pint not installed)",
        "receipt_sha256": None,
    }


def test_operator_sheets_keep_applications_and_decisions_apart_and_refuse_verdicts():
    conn = h.connection()
    result = h.load_bamf(conn)
    assert result["series"] == 3
    store = DemographicStore(conn)
    bamf = store.find_series(h.NS, provider="bamf")
    assert {s["definition"]["content"]["procedure"] for s in bamf} == {
        "application",
        "decision",
    }
    release = store.release(h.NS, bamf[0]["first_release_id"])
    assert (
        release["evidence_origin"] == "operator"
        and release["recorded_by"] == "operator-anna"
    )
    first = store.values(
        h.NS,
        next(s for s in bamf if s["indicator"] == "applications_first")["series_id"],
    )
    assert first["observations"][0]["locator"] == {"page": 5, "table": "Tabelle 1"}
    assert first["observations"][0]["value"] == "205100"
    sheet = json.loads(h.body(h.BAMF_SHEET))
    sheet["net"] = 7000
    with pytest.raises(DemographicError):
        store.import_sheet(h.NS, sheet, principal_id="x", scopes=h.SCOPES)
    with pytest.raises(DemographicError) as denied:
        store.import_sheet(
            h.NS, json.loads(h.body(h.BAMF_SHEET)), principal_id="x", scopes=h.READ_ONLY
        )
    assert denied.value.code == "unauthorized"
