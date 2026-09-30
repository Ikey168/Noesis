"""Capacity indicator, definition, definition-revision and observation records on the surveillance storage (HS02)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.health_capacity import CONTRACT, SCHEME, HealthCapacityError, HealthCapacityStore
from src.kb.surveillance import SurveillanceStore

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-health-capacity-record-v1.json").read_text())
NS = "clinical"
SCOPES = {"knowledge:clinical:read", "knowledge:clinical:write", f"namespace:{NS}:read", f"namespace:{NS}:write"}
DEFINITIONS = [
    {"key": "beds", "version": "2090", "valid_from": "2090-01-01", "valid_to": "2096-12-31",
     "text": "Beds staffed and available (fictional).", "locator": "https://example.org/def/2090",
     "icd_scope": None},
    {"key": "beds", "version": "2097", "valid_from": "2097-01-01", "valid_to": None,
     "text": "Beds staffed and available, day-care excluded (fictional).", "locator": "https://example.org/def/2097",
     "icd_scope": None},
]


def item(values, definitions=DEFINITIONS):
    return {
        "condition": {"scheme": SCHEME, "code": "beds", "label": "Hospital beds"},
        "indicator": {"code": "WHS6_102", "label": "Hospital beds (per 10 000 population)"},
        "geography": {"system": "iso3166-1-alpha3", "code": "DEU", "label": None, "code_list_version": None},
        "unit": {"label": "per 10 000 population", "published": "per 10 000 population"},
        "interval": "year",
        "kind": "observation",
        "dimensions": {},
        "denominator": None,
        "citations": [{"kind": "gho-indicator", "identifier": "WHS6_102"}],
        "case_definition": {"key": "beds", "revisions": copy.deepcopy(definitions)} if definitions else None,
        "delay_note": None,
        "values": [{"reference_period": p, "reporting_date": None, "value_text": t, "value": v, "lower": None,
                    "upper": None, "flags": f} for p, t, v, f in values],
    }


def header(published_on, body):
    raw = json.dumps(body, sort_keys=True).encode()
    return {"provider": "who-gho", "format": "who-gho-odata", "document": {"label": f"fixture {published_on}"},
            "native_revision": f"gho:WHS6_102:{published_on}", "published_on": published_on,
            "release_basis": "declared_publication", "file_sha256": hashlib.sha256(raw).hexdigest(),
            "item_count": 1, "evidence_origin": "fixture"}


@pytest.fixture
def store():
    conn = duckdb.connect()
    clock = iter(range(4_000_000_000_000, 4_000_000_000_000 + 10**9, 1000))
    return HealthCapacityStore(conn, now=lambda: next(clock))


def apply(store, published_on, values, definitions=DEFINITIONS):
    body = item(values, definitions)
    return store.series_store.apply_release(NS, header(published_on, body), [body], run_id=f"run:{published_on}",
                                            source_id="fixture")


def test_round_trip_carries_source_code_unit_definition_text_and_history(store):
    applied = apply(store, "2098-03-10", [("2096", "80.1", "80.1", []), ("2097", "", None, [])])
    assert applied["status"] == "applied"
    (indicator,) = store.indicators(NS, scopes=SCOPES)
    assert indicator["contract"] == CONTRACT and indicator["record_type"] == "capacity-indicator"
    assert (indicator["domain"], indicator["provider"], indicator["source_code"]) == ("beds", "who-gho", "WHS6_102")
    assert indicator["unit"]["label"] == "per 10 000 population"
    assert indicator["definition"]["text"] == DEFINITIONS[1]["text"]
    assert [r["version"] for r in indicator["definition_history"]] == ["2090", "2097"]
    assert all(r["retrieved_at_ms"] and r["declared_on"] == "2098-03-10" for r in indicator["definition_history"])
    assert not list(Draft7Validator(SCHEMA).iter_errors(indicator))
    observations = store.observations(NS, indicator["series_id"], scopes=SCOPES)
    first, missing = observations["values"]
    assert first["place"] == {"system": "iso3166-1-alpha3", "code": "DEU", "label": None}
    assert first["definition"]["version"] == "2090" and first["as_of_time"]["release_basis"] == "declared_publication"
    assert first["vintage_id"] == observations["vintage"]["vintage_id"]
    assert missing["value"] is None and missing["status"] == "unknown"
    for value in observations["values"]:
        assert not list(Draft7Validator(SCHEMA).iter_errors({"contract": CONTRACT, **value}))
    history = store.definition_history(NS, indicator["series_id"])
    assert history["record_type"] == "indicator-definition" and len(history["revisions"]) == 2


def test_vintages_coexist_and_the_as_of_time_selects_the_one_then_published(store):
    apply(store, "2098-03-10", [("2096", "80.1", "80.1", [])])
    apply(store, "2098-09-15", [("2096", "80.4", "80.4", ["comment: revised by the country"])])
    (indicator,) = store.indicators(NS, scopes=SCOPES)
    assert indicator["vintage_count"] == 2
    earlier = store.observations(NS, indicator["series_id"], scopes=SCOPES, as_of="2098-06-30")
    later = store.observations(NS, indicator["series_id"], scopes=SCOPES, as_of="2098-09-15")
    assert earlier["values"][0]["value"] == "80.1" and earlier["later_vintages"] == 1
    assert later["values"][0]["value"] == "80.4" and later["values"][0]["flags"] == ["comment: revised by the country"]
    assert later["vintage"]["revision_of"] == earlier["vintage"]["vintage_id"]
    before = store.observations(NS, indicator["series_id"], scopes=SCOPES, as_of="2098-01-01")
    assert before["status"] == "unavailable" and before["values"] == []
    named = store.observations(NS, indicator["series_id"], scopes=SCOPES, vintage_id=earlier["vintage"]["vintage_id"])
    assert named["values"][0]["value"] == "80.1"


def test_a_definition_change_marks_a_break_and_earlier_values_keep_their_definition(store):
    apply(store, "2098-03-10", [("2096", "80.1", "80.1", []), ("2097", "79.4", "79.4", [])])
    (indicator,) = store.indicators(NS, scopes=SCOPES)
    (brk,) = indicator["breaks"]
    assert brk["capacity_kind"] == "definition-break" and brk["period"] == "2097"
    assert (brk["from"], brk["to"]) == ("2090", "2097")
    values = store.observations(NS, indicator["series_id"], scopes=SCOPES)["values"]
    assert [v["definition"]["version"] for v in values] == ["2090", "2097"]
    assert [v["value"] for v in values] == ["80.1", "79.4"]  # nothing restated


def test_only_capacity_series_are_capacity_indicators_and_no_series_store_is_added(store):
    body = item([("2096", "1", "1", [])], definitions=None)
    body["condition"] = {"scheme": "gho-indicator", "code": "NOE_TB", "label": "Tuberculosis"}
    store.series_store.apply_release(NS, header("2098-01-01", body), [body], run_id="run", source_id="x")
    assert store.indicators(NS, scopes=SCOPES) == []
    (series_id,) = [r[0] for r in store.conn.execute("SELECT series_id FROM surveillance_series").fetchall()]
    with pytest.raises(HealthCapacityError):
        store.indicator(NS, series_id)
    tables = {r[0] for r in store.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert not {t for t in tables if "capacity" in t}
    assert isinstance(store.series_store, SurveillanceStore)
