"""NH02 (#2308): hazard records keep parameters as published, append revisions and quote alert validity."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb import hazards_records as hr
from src.kb.hazards_store import HazardStore, HazardStoreError, ms, valid_at

ROOT = Path(__file__).resolve().parents[3]
NS = "hazards"
SCOPES = {hr.READ_SCOPE, hr.WRITE_SCOPE, f"namespace:{NS}:read", f"namespace:{NS}:write"}


def quake(updated, magnitude, mag_type, depth, lon, lat, status="automatic"):
    return hr.event(
        "usgs", "us7000zz01", "M 6.1 - fictional Aegean Sea event", hazard_type="earthquake",
        source_url="https://earthquake.usgs.gov/earthquakes/eventpage/us7000zz01", revision_key=str(ms(updated)),
        published_at=updated, event_time="2099-08-10T03:12:45Z",
        parameters=[hr.parameter("magnitude", magnitude, "magnitude", qualifier=mag_type),
                    hr.parameter("depth", depth, "km")],
        geometry={"type": "Point", "coordinates": [lon, lat]}, status=status, status_scheme="usgs review status",
        identifiers={"ids": ["us7000zz01", "at00zz01"]})


def test_records_validate_against_the_published_schema_and_reject_excluded_fields():
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-hazard-record-v1.json").read_text())
    Draft7Validator.check_schema(schema)
    record = quake("2099-08-10T03:20:00Z", "5.8", "mb", "10", 26.8, 37.9)
    assert not list(Draft7Validator(schema).iter_errors(record))
    assert record["issuing_body"].startswith("U.S. Geological Survey")
    for field in ("risk_score", "damage_estimate", "prediction", "advice"):
        with pytest.raises(hr.HazardRecordError) as caught:
            hr.validate({**record, field: "x"})
        assert caught.value.code == "excluded_field"
    with pytest.raises(hr.HazardRecordError):  # floats would be rounded; values are quoted text
        hr.event("usgs", "x", "t", hazard_type="earthquake", source_url="https://earthquake.usgs.gov/x", revision_key="1",
                 published_at=None, event_time=None, parameters=[{"name": "magnitude", "value": 5.8, "unit": "magnitude"}])
    with pytest.raises(hr.HazardRecordError) as caught:  # EMSC never issues advisories
        hr.validate({**record, "provider": "emsc", "record_type": "advisory"})
    assert caught.value.code == "not_published_by_provider"
    estimate = {"contract": hr.CONTRACT, "record_type": "impact_estimate", "provider": "usgs", "hazard_type": "earthquake",
                "native_id": "us7000zz01:losspager", "title": "PAGER", "source_url": "https://earthquake.usgs.gov/x",
                "revision_key": "1", "event_native_id": "us7000zz01", "product": "losspager",
                "estimate": {"alert_level": "yellow"}}
    with pytest.raises(hr.HazardRecordError) as caught:
        hr.validate(estimate)
    assert caught.value.code == "product_version_required"
    valid = hr.validate({**estimate, "product_version": "1"})
    assert valid["notice"] == hr.ESTIMATE_NOTICE
    alert = {"contract": hr.CONTRACT, "record_type": "alert", "provider": "glofas", "hazard_type": "flood",
             "native_id": "n1", "title": "t", "source_url": "https://www.globalfloods.eu/x", "revision_key": "1",
             "level": "Flood notification", "issued_at": "2099-08-20T00:00:00Z", "modelled": True}
    with pytest.raises(hr.HazardRecordError) as caught:
        hr.validate(alert)
    assert caught.value.code == "model_required"


def test_revisions_append_never_overwrite_and_as_of_selects_the_revision_then_in_force():
    conn = duckdb.connect(":memory:")
    store = HazardStore(conn, now=lambda: ms("2099-09-01T00:00:00Z"))
    first = quake("2099-08-10T03:20:00Z", "5.8", "mb", "10", 26.8, 37.9)
    second = quake("2099-08-10T09:00:00Z", "6.1", "Mww", "12.4", 26.79, 37.91, status="reviewed")
    out = store.apply(NS, [first], run_id="r1", principal_id="op", scopes=SCOPES)
    assert out["revisions"] == 1 and out["places"] == 1
    assert store.apply(NS, [first], run_id="r1b", principal_id="op", scopes=SCOPES)["unchanged"] == 1
    store.apply(NS, [second], run_id="r2", principal_id="op", scopes=SCOPES)
    rid = store.find(NS, "usgs", "hazard_event", "us7000zz01")
    history = store.revisions(NS, rid, scopes=SCOPES)["revisions"]
    assert [h["content"]["status"] for h in history] == ["automatic", "reviewed"]
    changes = {c["parameter"]: c for c in history[1]["changes"]}
    assert changes["magnitude"]["before"] == {"value": "5.8", "unit": "magnitude"}
    assert changes["depth"]["after"] == {"value": "12.4", "unit": "km"}
    assert "geometry" in changes and "status" in changes
    early = store.record(NS, rid, scopes=SCOPES, as_of_ms=ms("2099-08-10T05:00:00Z"))
    assert early["revision"] == 1 and early["citation"]["published_at"] == "2099-08-10T03:20:00Z"
    assert store.record(NS, rid, scopes=SCOPES)["revision"] == 2
    before = store.record(NS, rid, scopes=SCOPES, as_of_ms=ms("2099-08-10T03:00:00Z"))
    assert before["content"] is None
    # A late, older version is history and never becomes current.
    stale = quake("2099-08-10T04:00:00Z", "6.0", "Mwr", "11", 26.8, 37.9)
    late = store.apply(NS, [stale], run_id="r3", principal_id="op", scopes=SCOPES)
    assert late["late"] == 1
    assert store.record(NS, rid, scopes=SCOPES)["content"]["status"] == "reviewed"
    assert len(store.revisions(NS, rid, scopes=SCOPES)["revisions"]) == 3
    # Every revision's geometry went through the geospatial owner.
    geometry_ids = {h["geometry_id"] for h in store.revisions(NS, rid, scopes=SCOPES)["revisions"]}
    assert all(g and g.startswith("geometry:") for g in geometry_ids)
    place = store.geo.place(NS, store.record(NS, rid, scopes=SCOPES)["place_id"], scopes={"knowledge:geospatial:read"})
    assert place["place_type"] == "hazard-event" and place["source_ids"] == {"usgs": "us7000zz01"}
    with pytest.raises(HazardStoreError):
        store.record(NS, rid, scopes={hr.READ_SCOPE})  # namespace access is required


def test_alert_validity_is_the_published_window_or_the_next_issue_never_inferred():
    content = {"issued_at": "2099-08-20T00:00:00Z", "valid_from": "2099-08-21T00:00:00Z", "valid_to": "2099-08-25T00:00:00Z"}
    assert valid_at(content, ms("2099-08-20T12:00:00Z"))["reason"] == "not yet valid"
    assert valid_at(content, ms("2099-08-22T00:00:00Z"))["in_force"] is True
    assert valid_at(content, ms("2099-08-26T00:00:00Z"))["reason"] == "expired"
    advisory = {"issued_at": "2099-09-01T09:00:00Z"}
    assert valid_at(advisory, ms("2099-09-01T10:00:00Z"), next_issued_ms=ms("2099-09-01T15:00:00Z"))["in_force"] is True
    superseded = valid_at(advisory, ms("2099-09-01T16:00:00Z"), next_issued_ms=ms("2099-09-01T15:00:00Z"))
    assert superseded["in_force"] is False and superseded["reason"].startswith("superseded")
    assert valid_at(advisory, ms("2099-09-03T00:00:00Z"))["basis"] == "open (no published expiry)"
