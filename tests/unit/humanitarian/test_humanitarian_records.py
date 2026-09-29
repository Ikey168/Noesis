"""HR02 (#2235): records carry source, revision and as-of time; revisions append; personal data is rejected."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.humanitarian_records import HumanitarianError, record_key, to_ms, validate_record
from src.kb.humanitarian_store import HumanitarianStore

ROOT = Path(__file__).resolve().parents[3]
NS = "humanitarian"
SCOPES = {"knowledge:humanitarian:read", "knowledge:humanitarian:write", f"namespace:{NS}:read", f"namespace:{NS}:write"}


def report(**overrides):
    value = {
        "record_type": "situation_report", "source": "reliefweb", "source_id": "9900001", "revision": "2098-05-02T10:00:00Z",
        "as_of": "2098-05-02T10:00:00Z", "title": "Fixture situation report No. 1",
        "source_url": "https://reliefweb.int/report/sudan/fixture-9900001",
        "publishers": [{"name": "Fixture Coordination Office", "shortname": "FCO"}], "report_date": "2098-05-01",
        "formats": ["Situation Report"], "disasters": [{"id": "99001", "glide": "CE-2098-000001-SDN"}],
        "places": [{"name": "Sudan", "code": "SDN", "scheme": "iso3", "level": "country"}],
        "body": {"locator": "https://reliefweb.int/report/sudan/fixture-9900001"},
    }
    value.update(overrides)
    return value


def event(**overrides):
    value = {
        "record_type": "conflict_event", "source": "ucdp-candidate", "source_id": "990001", "revision": "98.0.4",
        "as_of": "2098-05-20", "title": "Fixture event 990001", "coding_source": "ucdp-candidate",
        "dataset_version": "98.0.4", "coding_status": "candidate",
        "precision": {"where": {"code": 1, "scheme": "ucdp-where_prec"}, "when": {"code": 1, "scheme": "ucdp-date_prec"},
                      "type": {"code": 1, "scheme": "ucdp-type_of_violence"}},
        "actors": [{"role": "side_a", "label": "Fixture Armed Forces"}, {"role": "side_b", "label": "Fixture Militia"}],
        "counts": {"best": 3, "low": 2, "high": 5}, "date_start": "2098-05-10", "date_end": "2098-05-10",
        "location": {"latitude": 15.5, "longitude": 32.5, "published": True, "country_code": "625"},
        "places": [{"name": "Khartoum state", "code": None, "scheme": "name-only", "level": "admin1"}],
    }
    value.update(overrides)
    return value


@pytest.fixture()
def store():
    conn = duckdb.connect()
    clock = iter(range(1_000, 10_000_000, 1_000))
    yield HumanitarianStore(conn, now=lambda: next(clock))
    conn.close()


def test_records_validate_against_the_published_schema():
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-humanitarian-record-v1.json").read_text())
    Draft7Validator.check_schema(schema)
    for record in (report(), event()):
        assert not list(Draft7Validator(schema).iter_errors(validate_record(record)))


def test_revision_appends_and_never_overwrites(store):
    first = store.apply(NS, [report()], run_id="run-1", principal_id="alice", scopes=SCOPES, retrieved_at_ms=to_ms("2098-05-03"))
    assert [c["record_key"] for c in first["created"]] == ["reliefweb:situation_report:9900001"]
    same = store.apply(NS, [report()], run_id="run-2", principal_id="alice", scopes=SCOPES, retrieved_at_ms=to_ms("2098-05-04"))
    assert same["unchanged"] and not same["revised"]
    updated = report(revision="2098-05-06T08:00:00Z", as_of="2098-05-06T08:00:00Z", title="Fixture situation report No. 1 (updated)")
    revised = store.apply(NS, [updated], run_id="run-3", principal_id="alice", scopes=SCOPES, retrieved_at_ms=to_ms("2098-05-07"))
    assert revised["revised"][0]["changed_fields"] == ["as_of", "revision", "title"]
    history = store.history(NS, "reliefweb:situation_report:9900001", scopes=SCOPES)
    assert [h["seq"] for h in history] == [1, 2]
    assert history[1]["predecessor_revision_id"] == history[0]["revision_id"]
    assert history[0]["content"]["title"] == "Fixture situation report No. 1"  # the old revision is intact


def test_as_of_lookup_returns_the_revision_available_then(store):
    store.apply(NS, [report()], run_id="r1", principal_id="alice", scopes=SCOPES, retrieved_at_ms=to_ms("2098-05-03"))
    store.apply(NS, [report(revision="2098-05-06", as_of="2098-05-06", title="Updated")], run_id="r2",
                principal_id="alice", scopes=SCOPES, retrieved_at_ms=to_ms("2098-05-07"))
    key = "reliefweb:situation_report:9900001"
    assert store.revision(NS, key, scopes=SCOPES, as_of_ms=to_ms("2098-05-01")) is None
    assert store.revision(NS, key, scopes=SCOPES, as_of_ms=to_ms("2098-05-04"))["content"]["title"].startswith("Fixture")
    assert store.revision(NS, key, scopes=SCOPES, as_of_ms=to_ms("2098-05-06T12:00:00"))["content"]["title"] == "Updated"
    # Retrieval basis: on 2098-05-06 the update was published but not yet retrieved.
    assert store.revision(NS, key, scopes=SCOPES, as_of_ms=to_ms("2098-05-06T12:00:00"),
                          basis="retrieved")["content"]["title"].startswith("Fixture")


@pytest.mark.parametrize("field,value", [
    ("victim_name", "A. Person"), ("notes", "free text naming someone"), ("source_headline", "headline"),
])
def test_personal_data_fields_are_rejected_anywhere(store, field, value):
    with pytest.raises(HumanitarianError) as caught:
        validate_record(event(location={"latitude": 1.0, "longitude": 2.0, field: value}))
    assert caught.value.code == "personal_data"
    result = store.apply(NS, [report(publishers=[{"name": "Org", "contact_email": "x@example.org"}])], run_id="r",
                         principal_id="alice", scopes=SCOPES)
    assert result["rejected"][0]["code"] == "personal_data" and not result["created"]


def test_conflict_events_keep_coding_source_precision_and_published_counts():
    stored = validate_record(event())
    assert stored["precision"]["where"] == {"code": 1, "scheme": "ucdp-where_prec"}
    assert stored["counts"] == {"best": 3, "low": 2, "high": 5}
    with pytest.raises(HumanitarianError) as merged:
        validate_record(event(counts={"best": 3, "true_count": 4}))
    assert merged.value.code == "derived_count"
    with pytest.raises(HumanitarianError):
        validate_record(event(coding_source="acled"))  # a UCDP record cannot claim another coder
    with pytest.raises(HumanitarianError):
        validate_record(event(actors=[{"label": "Fixture Militia", "entity_id": "ent-1"}]))


def test_places_are_stored_as_published_never_as_geospatial_ids():
    with pytest.raises(HumanitarianError):
        validate_record(report(places=[{"name": "Sudan", "code": "SDN", "scheme": "iso3", "place_id": "place:1"}]))


def test_bodies_are_never_mirrored_and_candidate_and_final_share_one_key():
    with pytest.raises(HumanitarianError) as caught:
        validate_record(report(body={"text": "full text"}))
    assert caught.value.code == "mirrored_body"
    assert record_key(event()) == record_key(event(source="ucdp-ged", coding_source="ucdp-ged")) == "ucdp:conflict_event:990001"


def test_complete_final_release_drops_a_candidate_as_a_revision_not_a_deletion(store):
    store.apply(NS, [event(), event(source_id="990002", title="Fixture event 990002")], run_id="c", principal_id="alice",
                scopes=SCOPES)
    final = event(source="ucdp-ged", coding_source="ucdp-ged", coding_status="final", dataset_version="99.1",
                  revision="99.1", as_of="2099-06-01", counts={"best": 4, "low": 2, "high": 5})
    release = {"record_type": "event_release", "source": "ucdp-ged", "source_id": "ged-99.1-625", "revision": "99.1",
               "as_of": "2099-06-01", "title": "UCDP GED 99.1", "coding_source": "ucdp-ged", "dataset_version": "99.1",
               "complete": True, "country": "625", "window": {"start": "2098-01-01", "end": "2098-12-31"},
               "event_ids": ["990001"]}
    result = store.apply(NS, [final, release], run_id="g", principal_id="alice", scopes=SCOPES)
    assert result["revised"][0]["changed_fields"] == ["as_of", "coding_source", "coding_status", "counts",
                                                      "dataset_version", "revision", "source"]
    assert [d["record_key"] for d in result["dropped"]] == ["ucdp:conflict_event:990002"]
    history = store.history(NS, "ucdp:conflict_event:990002", scopes=SCOPES)
    assert [h["content"]["coding_status"] for h in history] == ["candidate", "dropped-in-release"]
