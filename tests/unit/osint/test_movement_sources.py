"""Bounded, receipted movement acquisition through the source-pack runtime (#2242, #2247, #2252, #2258, #2263)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.osint_movement_sources import (
    EXCLUDED_SOURCES,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    selection_entries,
    source_contracts,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.osint.movements import MovementStore
from tests.unit.osint.movement_harness import FIXTURES, NS, ROOT, SOURCES, Env, faa_request, manifest, pages


@pytest.fixture(scope="module")
def env():
    return Env().loaded()


def _statements(env, record_type=None, provider=None, subject=None):
    store = MovementStore(env.conn, initialize=False)
    out = []
    for record in store.records(NS, record_type=record_type, provider=provider,
                                subject_keys=[subject] if subject else None):
        out.append(store.latest(NS, record["record_id"])["statement"])
    return out


def test_every_provider_has_a_recorded_decision_and_is_unverified_live():
    assert set(PROVIDER_CONTRACTS) == set(PROVIDERS) == set(LIVE_VERIFICATION)
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())
    contracts = source_contracts()
    assert "mirroring" in contracts["rejected"] and {"adsb-exchange", "noaa-marinecadastre-ais"} <= set(EXCLUDED_SOURCES)
    assert contracts["providers"]["opensky"]["bounds"]["max_window_hours"] == 48


def test_fixtures_replay_offline_to_their_pinned_output():
    value = manifest()
    report = SourcePackConformance(ROOT).offline(value)
    ours = [r for r in report["sources"] if r["source_id"] in FIXTURES]
    assert len(ours) == len(SOURCES) and all(r["valid"] for r in ours)


def test_runs_are_receipted_and_bounded(env):
    runs = {r["source_id"]: r for r in MovementStore(env.conn, initialize=False).runs(NS)}
    assert set(runs) == set(SOURCES) and all(r["status"] == "complete" for r in runs.values())
    faa = {o["selection"]["n_number"]: o for o in runs["movements-faa-registry"]["outcomes"]}
    assert faa["N907EX"]["outcome"] == "privacy_refused"  # declared LADD: no request made
    assert faa["N903EX"]["outcome"] == "withheld"
    assert "Street" in faa["N901EX"]["excluded_fields_dropped"]  # owner address never parsed
    opensky = {o["selection"]["icao24"]: o for o in runs["movements-opensky"]["outcomes"]}
    assert opensky["a0f1c3"]["refused"] == {"private_aircraft_refused": 6}
    assert opensky["a0f1f6"]["outcome"] == "privacy_refused"


def test_faa_registry_keyed_by_n_number_and_mode_s_hex_with_person_registrant_flagged(env):
    records = {s["subject"]["key"]: s for s in _statements(env, "registry_record", "faa-registry")}
    assert set(records) == {"aircraft:registration:N901EX", "aircraft:registration:N902EX",
                            "aircraft:registration:N904EX"}  # N903EX withheld, N907EX refused
    jet = records["aircraft:registration:N901EX"]
    assert {(i["scheme"], i["value"]) for i in jet["identifiers"]} >= {("registration", "N901EX"),
                                                                       ("icao24", "a0f1b2")}
    assert jet["as_published"]["registrant"] == {"kind": "LLC", "name": "EXAMPLE AIR CHARTER LLC",
                                                 "natural_person": False,
                                                 "note": "as published; never a lookup key"}
    assert "street" not in json.dumps(jet).casefold()
    person = records["aircraft:registration:N902EX"]
    assert person["as_published"]["registrant"]["natural_person"] is True
    assert all(i["scheme"] != "name" for i in person["identifiers"])
    assert records["aircraft:registration:N904EX"]["effective"]["event"] == "deregistered"
    store = MovementStore(env.conn, initialize=False)
    assert store.refusal(NS, "registration", "N903EX")["programme"] == "faa-privacy"
    assert store.refusal(NS, "registration", "N907EX")["programme"] == "faa-ladd"


def test_faa_changes_are_dated_revisions_and_deregistration_never_deletes():
    env = Env().loaded()
    body = pages("movements-faa-registry")[0]["body"].replace("Valid", "Deregistered").replace(
        "</table>", '<tr><td data-label="Cancel Date">06/01/2099</td></tr></table>')
    result = env.run("movements-2", source_ids=["movements-faa-registry"],
                     overrides={faa_request("N901EX"): {"body": body}})
    assert result["status"] == "complete"
    store = MovementStore(env.conn, initialize=False)
    (record,) = store.records(NS, subject_keys=["aircraft:registration:N901EX"], record_type="registry_record")
    history = store.revisions(NS, record["record_id"])
    assert [r["event"] for r in history] == ["registered", "deregistered"]
    assert history[1]["effective_from"] == "2099-06-01" and history[0]["statement"]["as_published"]["status"] == \
        "Valid"


def test_uk_registry_keeps_the_nationality_prefix_and_published_address_only(env):
    records = {s["subject"]["key"]: s for s in _statements(env, "registry_record", "uk-caa-ginfo")}
    company = records["aircraft:registration:G-EXMP"]
    assert company["as_published"]["nationality_prefix"] == "G" and company["as_published"]["icao24"] == "4007f1"
    private = records["aircraft:registration:G-EXMQ"]
    assert "icao24" not in private["as_published"]  # not published: not stored
    assert private["effective"]["event"] == "deregistered" and private["as_published"]["registrant"][
        "natural_person"] is True
    assert "Example Lane" not in json.dumps(private)


def test_opensky_samples_are_bounded_windows_with_gaps_and_estimated_airports(env):
    subject = "aircraft:icao24:a0f1b2"
    (window,) = _statements(env, "sample_window", "opensky", subject)
    assert window["coverage"]["status"] == "positions_observed" and window["coverage"]["samples_stored"] == 10
    assert any(g["from"] == "2099-05-01T08:30:00Z" and g["to"] == "2099-05-01T09:20:00Z"
               for g in window["coverage"]["gaps"])
    assert window["bound"]["max_window_hours"] == 48 and window["bound"]["licence_decision"].startswith("docs/")
    samples = _statements(env, "position_sample", "opensky", subject)
    assert len(samples) == 10 and all(s["window"]["window_id"] == window["window"]["window_id"] for s in samples)
    assert all(s["as_published"]["receiver_category"].startswith("OpenSky") for s in samples)
    calls = _statements(env, "call", "opensky", subject)
    assert {(c["as_published"]["event"], c["as_published"]["facility"]["code"]) for c in calls} == {
        ("departure", "KEXA"), ("arrival", "KEXB")}
    assert all(c["as_published"]["confidence"].startswith("estimated by OpenSky") for c in calls)
    (empty,) = _statements(env, "sample_window", "opensky", "aircraft:icao24:a0f1e5")
    assert empty["coverage"]["status"] == "no_coverage_observed" and empty["coverage"]["samples_stored"] == 0
    assert not _statements(env, subject="aircraft:icao24:a0f1c3")  # natural-person aircraft: refused
    assert not _statements(env, subject="aircraft:icao24:a0f1f6")  # declared PIA: never requested


def test_over_bound_and_area_selections_are_refused_before_any_request():
    value = manifest()
    source = copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == "movements-opensky"))
    source["osint_movements"]["selection"] = [{"icao24": "a0f1b2", "begin": "2099-05-01T00:00:00Z",
                                               "end": "2099-05-04T00:00:00Z"}]
    with pytest.raises(SourcePackError, match="over_bound"):
        selection_entries(source)
    source["osint_movements"]["selection"] = [{"begin": "2099-05-01T00:00:00Z", "end": "2099-05-01T06:00:00Z"}]
    with pytest.raises(SourcePackError, match="one ICAO 24-bit address"):
        selection_entries(source)


def test_gfw_port_visits_are_published_calls_with_confidence_and_no_tracks(env):
    subject = "vessel:gfw:a1b2c3d4-0002-4000-8000-000000000002"
    calls = _statements(env, "call", "gfw-port-visits", subject)
    assert sorted(c["as_published"]["confidence"] for c in calls) == ["2", "4"]
    assert all(c["as_published"]["status"] == "source-published" for c in calls)
    assert not _statements(env, "position_sample", "gfw-port-visits")
    (identity,) = _statements(env, "vessel_identity", "gfw-port-visits", subject)
    assert {i["scheme"] for i in identity["identifiers"]} == {"gfw_vessel_id", "mmsi"}
    (window,) = _statements(env, "sample_window", "gfw-port-visits",
                            "vessel:gfw:a1b2c3d4-0001-4000-8000-000000000001")
    assert window["coverage"]["status"] == "no_events_published"


def test_open_ais_stores_the_named_vessel_only_and_aggregates_stay_aggregates(env):
    samples = _statements(env, "position_sample", "kystdatahuset-ais")
    assert samples and {s["subject"]["key"] for s in samples} == {"vessel:mmsi:671000002"}
    (identity,) = _statements(env, "vessel_identity", "kystdatahuset-ais")
    assert {(i["scheme"], i["value"]) for i in identity["identifiers"]} == {("mmsi", "671000002"),
                                                                            ("imo", "9000027")}
    (empty,) = _statements(env, "sample_window", "kystdatahuset-ais", "vessel:mmsi:257000009")
    assert empty["coverage"]["status"] == "no_coverage_observed"
    aggregates = _statements(env, "aggregate", "unctad-port-calls")
    assert len(aggregates) == 4 and {a["subject"]["key"] for a in aggregates} == {"area:unctad-economy:578",
                                                                                  "area:unctad-economy:724"}
    assert all("mmsi" not in json.dumps(a) and a["as_published"]["port_calls"] for a in aggregates)


def test_replaying_a_run_adds_no_revision(env):
    store = MovementStore(env.conn, initialize=False)
    before = store.generation(NS)
    assert env.run("movements-replay")["status"] == "complete"
    assert store.generation(NS) == before
