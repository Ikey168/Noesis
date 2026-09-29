"""AF02: agri-food records round-trip, keep flags verbatim and let vintages coexist (#2333)."""

from __future__ import annotations

import duckdb
import pytest

from src.ingestion.connectors.dataset.store import ObservationStore
from src.kb.agrifood_records import (
    SCHEMA_VERSIONS,
    AgrifoodError,
    decimal_text,
    figure,
    flag,
    validate_statement,
)
from src.kb.agrifood_store import AgrifoodStore

NS = "global"
SOURCE = {"url": "https://example.org/fixture", "locator": "/data/0", "attribution": "Source: test",
          "evidence_origin": "fixture"}


def _figure(value="100", *, release="r1", released_at="2024-01-01", code="A", period_type="calendar-year",
            period="2023", status="reported"):
    return figure(
        "observation", "faostat", "QCL", commodity={"scheme": "faostat-item", "code": "56", "label": "Maize (corn)"},
        place={"scheme": "fao-area", "code": "231", "label": "United States of America", "level": "country"},
        measure={"kind": "production", "element": "Production", "element_code": "5510", "label": "Production"},
        unit="t", period={"type": period_type, "value": period, "key": period, "reference": None, "start": None,
                          "end": None, "definition": None},
        value={"text": value, "number": decimal_text(value) if status == "reported" else None, "status": status},
        flag_value=flag("faostat-flags", code, note="as noted"), estimate_type="observation",
        release={"key": release, "released_at": released_at, "basis": "test release"},
        as_published={"Value": value, "Flag": code}, source=SOURCE)


@pytest.fixture()
def store():
    conn = duckdb.connect(":memory:")
    clock = iter(range(1_000, 10_000_000, 1_000))
    yield AgrifoodStore(conn, now=lambda: next(clock))
    conn.close()


def test_schema_versions_and_statement_round_trip(store):
    assert SCHEMA_VERSIONS["agrifood-record"] == "1.0.0"
    statement = _figure()
    assert validate_statement(statement) == statement
    store.apply(NS, statement)
    (series,) = store.series_list(NS)
    assert (series["commodity_code"], series["place_code"], series["element"], series["unit"],
            series["period_type"]) == ("56", "231", "Production", "t", "calendar-year")
    (vintage,) = store.vintages(NS, series["series_id"])
    assert vintage["released_at"] == "2024-01-01" and vintage["release_key"] == "r1"
    (value,) = store.values(NS, vintage["vintage_id"])
    assert value["statement"] == statement  # round trip through the store
    assert value["value"] == "100" and value["value_text"] == "100" and value["status"] == "reported"


def test_flags_are_kept_verbatim_with_classes():
    assert flag("faostat-flags", "I", label="Imputed value", note="n")["classes"] == ["imputed"]
    combined = flag("eurostat-obs-flags", "ep")
    assert combined["code"] == "ep" and combined["classes"] == ["estimated", "provisional"]
    unknown = flag("faostat-flags", "Q", label="Some new flag")
    assert unknown["code"] == "Q" and unknown["label"] == "Some new flag" and unknown["classes"] == ["unknown"]
    withheld = flag("nass-value-codes", "(D)")
    assert withheld["classes"] == ["withheld"] and "disclosing" in withheld["label"]
    assert flag("agri-food-portal", None)["classes"] == ["not-flagged"]


def test_values_and_periods_are_checked():
    with pytest.raises(AgrifoodError):
        _figure("(D)", status="reported")  # a reported figure carries a number
    withheld = _figure("(D)", status="withheld", code=None)
    assert withheld["value"]["number"] is None
    marketing = _figure(period_type="marketing-year", period="2023/24")
    assert marketing["period"] == {**marketing["period"], "type": "marketing-year", "value": "2023/24"}
    assert marketing["series_key"] != _figure()["series_key"]  # period types never share a series
    bad = dict(_figure())
    bad["period"] = {**bad["period"], "type": "fiscal-year"}
    with pytest.raises(AgrifoodError):
        validate_statement(bad)
    tampered = dict(_figure())
    tampered["food_security_score"] = 3
    with pytest.raises(AgrifoodError):
        validate_statement(tampered)
    assert decimal_text("13,651,159,000") == "13651159000" and decimal_text("€226.00") == "226"
    assert decimal_text("(D)") is None


def test_vintages_coexist_and_never_overwrite(store):
    first = store.apply(NS, _figure("100"))
    assert first["counts"]["created"] == 1
    assert store.apply(NS, _figure("100"))["counts"]["unchanged"] == 1  # replay adds nothing
    revised = store.apply(NS, _figure("105", release="r2", released_at="2024-06-01", code="E"))
    assert revised["counts"]["revised"] == 1
    (series,) = store.series_list(NS)
    vintages = store.vintages(NS, series["series_id"])
    assert [v["release_key"] for v in vintages] == ["r1", "r2"]
    values = [store.values(NS, v["vintage_id"])[0] for v in vintages]
    assert [(v["value"], v["flag"]["code"]) for v in values] == [("100", "A"), ("105", "E")]
    with pytest.raises(AgrifoodError) as caught:
        store.apply(NS, _figure("999", release="r2", released_at="2024-06-01"))
    assert caught.value.code == "vintage_conflict"
    # The Economics ObservationStore holds each vintage at its release time.
    observations = ObservationStore(store.conn)
    assert len(observations.vintages(series["series_id"])) == 2
    assert [o.value for o in observations.get_observations(series["series_id"], as_of=vintages[0]["as_of_ms"])] == [
        100.0]
    assert observations.get_series(series["series_id"])["as_of"] == vintages[1]["as_of_ms"]


def test_a_vintage_without_release_time_is_dated_by_first_retrieval(store):
    store.apply(NS, _figure(release="content:abc", released_at=None), observed_at_ms=42_000)
    (series,) = store.series_list(NS)
    (vintage,) = store.vintages(NS, series["series_id"])
    assert vintage["released_at"] is None and vintage["as_of_ms"] == 42_000
    assert "first retrieval" in vintage["release_basis"]
