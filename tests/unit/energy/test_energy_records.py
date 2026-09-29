"""EN02 (#2234): energy records, schema versions and vintage coexistence."""

from __future__ import annotations

import copy

import duckdb
import pytest

from src.kb import energy_records as enr
from src.kb.energy_store import EnergyStore, EnergyStoreError
from tests.unit.energy.harness import NS, SCOPES, acquire

LICENCE = {"id": "fixture", "terms_url": "https://example.org/terms"}


def _record(**overrides):
    base = dict(source_url="https://example.org/", attribution="Fixture publisher", licence=LICENCE,
                subject={"kind": "country", "scheme": "iso3166-alpha3", "code": "DEU", "name": "Germany"},
                unit="TWh", resolution="P1M", reference_period={"start": "2026-06", "end": "2026-07"},
                values=[{"start": "2026-06", "end": "2026-07", "value": "10.5", "flags": {"row": 1}}],
                release={"key": "r1", "released_at": "2026-08-28", "basis": "declared_release", "label": "r1",
                         "revision": None},
                status="provisional", retrieved_at="2026-09-25T06:00:00Z", facets={"fuel": {"code": "Solar"}})
    base.update(overrides)
    return enr.record("generation", "ember", "ember:fixture", "fixture:DEU:Solar", "Fixture", **base)


def test_round_trip_and_schema_validation():
    value = _record()
    assert enr.validate(copy.deepcopy(value)) == value
    assert enr.validate_against_schema(value) == []
    for key in ("unit", "resolution", "reference_period", "release", "status", "retrieved_at", "source_url"):
        assert key in value
    broken = {**value, "status": "final-ish"}
    assert enr.validate_against_schema(broken)
    with pytest.raises(enr.EnergyRecordError):
        enr.validate(broken)


def test_observations_only_and_published_text_only():
    with pytest.raises(enr.EnergyRecordError) as forecast:
        enr.record(
            "load", "eia", "eia:rto/region-data", "x", "x", source_url="https://api.eia.gov/", attribution="EIA",
            licence=LICENCE, subject={"kind": "balancing-area", "scheme": "eia-ba", "code": "CISO", "name": None},
            unit="MWh", resolution="PT1H", reference_period={"start": "2026-09-24T00:00:00Z", "end": None},
            values=[], release={"key": "k", "basis": "retrieval_time"}, status="unknown",
            retrieved_at="2026-09-25T06:00:00Z", facets={"series_type": "DF"})
    assert forecast.value.code == "forecast_refused"
    with pytest.raises(enr.EnergyRecordError):
        _record(values=[{"start": "2026-06", "end": None, "value": 10.5, "flags": {}}])
    with pytest.raises(enr.EnergyRecordError) as derived:
        _record(publisher_figures={"emissions_estimate": "1"})
    assert derived.value.code == "derived_value_refused"
    with pytest.raises(enr.EnergyRecordError):
        _record(release={"key": "k", "released_at": None, "basis": "declared_release"})


def test_capacity_flows_and_balances_carry_their_structure():
    capacity = enr.record(
        "capacity", "eia", "eia:operating-generator-capacity", "plant:1:generator:G1", "Unit",
        source_url="https://api.eia.gov/", attribution="EIA", licence=LICENCE,
        subject={"kind": "unit", "scheme": "eia-generator", "code": "1:G1", "name": "Plant"}, unit="MW",
        resolution="P1M", reference_period={"start": "2026-06", "end": "2026-07"},
        values=[{"start": "2026-06", "end": "2026-07", "value": "50", "flags": {}}],
        release={"key": "k", "released_at": "2026-08-26", "basis": "declared_release"}, status="provisional",
        retrieved_at="2026-09-25T06:00:00Z",
        capacity={"level": "unit", "effective_from": "2021-08", "effective_to": "2026-07", "operating_status": "RE"})
    assert capacity["capacity"]["effective_to"] == "2026-07" and enr.validate_against_schema(capacity) == []
    with pytest.raises(enr.EnergyRecordError):
        enr.record("capacity", **{**_kw(), "capacity": {"level": "nation"}})
    with pytest.raises(enr.EnergyRecordError):
        enr.record("cross_border_flow", **{**_kw(), "counterpart": None})
    with pytest.raises(enr.EnergyRecordError):
        enr.record("energy_balance", **{**_kw(), "facets": {"nrg_bal": "GIC"}})


def _kw():
    return dict(provider="ember", dataset="d", native_id="n", title="t", source_url="https://example.org/",
                attribution="a", licence=LICENCE, subject={"kind": "country", "scheme": "iso3166-alpha3", "code": "DEU"},
                unit="MW", resolution="P1Y", reference_period={"start": "2026"}, values=[],
                release={"key": "k", "basis": "retrieval_time"}, status="unknown", retrieved_at="2026-09-25T06:00:00Z")


def test_provisional_and_revised_vintages_coexist_and_are_never_overwritten():
    conn = duckdb.connect()
    first = acquire(conn, "energy-entsoe")
    again = acquire(conn, "energy-entsoe")
    assert first["ok"] and again["applied"]["vintages"] == 0 and again["applied"]["unchanged"] == 9
    revised = acquire(conn, "entsoe_revision_2")
    assert revised["applied"]["vintages"] == 2  # both A75 series are republished under revision 2
    store = EnergyStore(conn)
    wind = next(s for s in store.series(NS, scopes=SCOPES, provider="entsoe", record_type="generation")
                if s["facets"]["fuel"]["code"] == "B19")
    vintages = store.vintages(NS, wind["series_id"], scopes=SCOPES)
    assert [v["status"] for v in vintages] == ["provisional", "revised"]
    assert vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    assert [v["value"] for v in store.values(vintages[0]["vintage_id"])][1] == "9204"
    assert [v["value"] for v in store.values(vintages[1]["vintage_id"])][1] == "9240"
    assert vintages[0]["record"]["release"]["revision"] == "1" and vintages[1]["record"]["release"]["revision"] == "2"


def test_store_requires_scope_and_caller_namespace():
    conn = duckdb.connect()
    store = EnergyStore(conn)
    with pytest.raises(EnergyStoreError):
        store.apply(NS, [_record()], run_id="r", principal_id="p", scopes={"knowledge:energy:read"})
    with pytest.raises(EnergyStoreError):
        store.apply("global", [_record()], run_id="r", principal_id="p", scopes={"operator"})


def test_schema_versions_register_in_the_schema_registry():
    conn = duckdb.connect()
    modules = enr.register_schemas(conn, principal_id="svc", scopes={"operator", "knowledge:schema:register"})
    assert len(modules) == 1
    again = enr.register_schemas(conn, principal_id="svc", scopes={"operator", "knowledge:schema:register"})
    assert len(again) == 1
