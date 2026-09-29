"""NH14 (#2372): offline place-to-hazard-events acceptance journey for the Natural Hazards pack.

Recorded (authored, fictional) fixtures for every acquired source — USGS events and PAGER, EMSC, GDACS, NHC
advisories, EFFIS burnt areas and key-gated GloFAS notifications — replay with sockets blocked: earlier states
through the real adapter and projector, the pinned latest state through the source-pack runtime. A place and window
reach cited hazard events with the revision in force at the as-of date and their full parameter history,
cross-source correspondents shown side by side without merging, advisories with their supersession chain, alerts
in force as issued, and a place with none on record. The answers carry no prediction, risk score, derived damage
estimate or advice.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.hazards_bundle import BUNDLE, export_bundle
from src.kb.hazards_identity import HazardIdentity
from src.kb.hazards_monitoring import LABEL
from src.kb.hazards_queries import NONE_ON_RECORD, NOT_COVERED, alerts_in_force, events_affecting
from src.kb.hazards_store import HazardStore
from tests.unit.hazards import harness as h
from tests.unit.hazards.test_hazards_identity_links import SAMOS, by_pair
from tests.unit.hazards.test_hazards_queries import ALGARVE, BAHAMAS, KARLOVASI, place

FORBIDDEN_KEYS = {"risk_score", "risk", "damage_estimate", "prediction", "forecast_by_noesis", "advice",
                  "safety_advice", "evacuation", "attribution"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _runtime(conn):
    clock = {"now": h.ms("2099-09-02T00:00:00Z")}

    def tick():
        clock["now"] += 1000
        return clock["now"]

    return SourcePackRuntime(conn, now=tick, sleep=lambda _delay: None)


def _run_pack(conn, key):
    runtime = _runtime(conn)
    adapters = runtime.fixture_adapters("natural-hazards", h.ROOT)
    return runtime.run({"pack_id": "natural-hazards", "run_key": key, "operation": "observe",
                        "source_ids": sorted(adapters), "max_results": 300, "max_bytes": 10_000_000,
                        "timeout_ms": 30_000},
                       principal_id="operator", adapters=adapters, dns_resolver=lambda _host: ["93.184.216.34"],
                       secret_resolver=lambda ref: "fixture-glofas-token" if ref == "NOESIS_GLOFAS_TOKEN" else None)


def test_place_and_window_to_cited_events_with_revision_history_correspondents_and_alerts_as_issued():
    conn = h.connection()
    # Earlier publisher states, acquired first through the real adapter and projector.
    for provider in ("usgs", "emsc", "gdacs", "effis", "nhc"):
        h.apply(conn, provider, h.EARLIER[provider], at="2099-08-10T04:30:00Z" if provider != "nhc"
                else "2099-09-01T09:30:00Z")
    # The pinned latest state of every source through the source-pack runtime and its hazard projector.
    manifest = h.manifest()
    SourcePackStore(conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
    runtime = _runtime(conn)
    for source in manifest["sources"]:
        runtime.accept_license("natural-hazards", source["source_id"], principal_id="operator")
    receipt = _run_pack(conn, "latest")
    assert receipt["status"] == "complete", receipt
    store = HazardStore(conn, initialize=False)
    assert {p for p in h.SOURCES if store.provider_state(h.NS, p)["acquired"]} == set(h.SOURCES)
    counts = {p: len(store.record_ids(h.NS, provider=p)) for p in h.SOURCES}
    assert counts == {"usgs": 4, "emsc": 1, "gdacs": 10, "nhc": 5, "effis": 1, "glofas": 1}

    places = {"samos": place(store, "Samos", SAMOS, "GRC"), "bahamas": place(store, "Northwestern Bahamas", BAHAMAS, "BHS"),
              "algarve": place(store, "Algarve", ALGARVE, "PRT")}
    identity = HazardIdentity(conn, now=lambda: h.ms("2099-09-03T00:00:00Z"))
    identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    pairs = by_pair(identity, store)
    for key in ((frozenset({"us7000zz01", "20990810_0000031"}), "proximity"),
                (frozenset({"us7000zz01", "EQ1400001"}), "shared_identifier")):
        identity.review(h.NS, pairs[key]["correspondence_id"], "accept", "same physical event", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)
    usgs_id = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    usgs_before = store.record(h.NS, usgs_id, scopes=h.SCOPES)["content"]

    def events(**kwargs):
        return events_affecting(conn, h.NS, scopes=h.SCOPES, principal_id="analyst", **kwargs)

    # As-of revision selection: early on 10 August the automatic mb 5.8 revision was in force.
    early = events(point=KARLOVASI, radius_m=50_000, start="2099-08-01", end="2099-08-31", as_of="2099-08-10T05:00:00Z")
    quake = next(e for e in early["events"] if e["native_id"] == "us7000zz01")
    assert quake["status"] == "automatic" and quake["revision_used"]["later_revisions_exist"]
    assert quake["revision_used"]["as_of_basis"] == "publisher"
    latest = events(point=KARLOVASI, radius_m=50_000, start="2099-08-01", end="2099-08-31")
    quake = next(e for e in latest["events"] if e["native_id"] == "us7000zz01")
    assert quake["status"] == "reviewed" and len(quake["revision_history"]) == 2
    assert {c["parameter"] for c in quake["revision_history"][1]["changes"]} >= {"magnitude", "depth", "status"}
    # Cross-source correspondence without merging: each publisher's own parameters, side by side.
    beside = {c["provider"]: {p["name"]: p["value"] for p in c["parameters"]} for c in quake["correspondents"]}
    assert beside["emsc"]["magnitude"] == "6.0" and beside["gdacs"]["severity"] == "6.1"
    assert {p["name"]: p["value"] for p in quake["parameters"]}["magnitude"] == "6.1"
    assert store.record(h.NS, usgs_id, scopes=h.SCOPES)["content"] == usgs_before
    # A place boundary: the burnt area is inside Samos, the offshore epicentre is not.
    samos = events(place_id=places["samos"], start="2099-08-01", end="2099-08-31")
    fire = next(e for e in samos["events"] if e["native_id"] == "99001")
    assert {p["name"]: p["value"] for p in fire["parameters"]}["burnt_area"] == "342"
    assert "us7000zz01" not in {e["native_id"] for e in samos["events"]}
    # None on record vs source not covered.
    algarve = events(place_id=places["algarve"], start="2099-08-01", end="2099-08-31")
    assert algarve["events"] == [] and algarve["answer"] == NONE_ON_RECORD
    assert events(bbox=[130, -30, 140, -20], start="2099-08-01", end="2099-08-31")["answer"] == NOT_COVERED

    # Advisory supersession and alerts in force as issued.
    bahamas = alerts_in_force(conn, h.NS, place_id=places["bahamas"], at="2099-09-01T13:00:00Z", scopes=h.SCOPES,
                              principal_id="analyst")
    (advisory,) = [a for a in bahamas["alerts"] if a["provider"] == "nhc"]
    assert advisory["advisory_number"] == "12A" and advisory["superseded_numbers"] == ["11", "12"]
    assert advisory["validity"]["window"] == ["2099-09-01T12:00:00Z", "2099-09-01T15:00:00Z"]
    assert advisory["matched_by"]["quoted_area"] == "Northwestern Bahamas"
    (storm,) = [a for a in bahamas["alerts"] if a["provider"] == "gdacs"]
    assert storm["level"] == "Red" and storm["citation"]["issuing_body"].startswith("Global Disaster Alert")
    flood = alerts_in_force(conn, h.NS, point=[26.3, 41.6], radius_m=10_000, country="GRC", at="2099-08-22T00:00:00Z",
                            scopes=h.SCOPES, principal_id="analyst")
    (notice,) = [a for a in flood["alerts"] if a["provider"] == "glofas"]
    assert notice["modelled"] and notice["model"]["notice"].startswith("the publisher's modelled output")
    quiet = alerts_in_force(conn, h.NS, place_id=places["algarve"], at="2099-08-22T00:00:00Z", scopes=h.SCOPES,
                            principal_id="analyst")
    assert quiet["alerts"] == [] and quiet["per_provider"]["glofas"] == "source not covered"

    # Every answer is cited and exported with source, revision and as-of time.
    exported = export_bundle(latest, alerts=bahamas, created_at_ms=h.ms("2099-09-03T00:00:00Z"))
    assert exported["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    assert len(exported["citations"]) >= len(latest["events"]) + len(bahamas["alerts"])
    assert all(c["source_url"].startswith("https://") and c["revision_id"] and c["published_at"]
               for c in exported["citations"])

    # Exclusions: no prediction, risk score, derived damage estimate or advice anywhere in the answers.
    for answer in (early, latest, samos, algarve, bahamas, flood, exported):
        assert not FORBIDDEN_KEYS & set(_keys(json.loads(json.dumps(answer))))
    assert early["exclusions"] == list(BUNDLE["never"]) == bahamas["exclusions"]
    assert "not a warning" in bahamas["notice"] and "not a warning" in LABEL
    pager = store.record(h.NS, store.find(h.NS, "usgs", "impact_estimate", "us7000zz01:losspager:usus7000zz01"),
                         scopes=h.SCOPES)["content"]
    assert pager["notice"].startswith("the publisher's own estimate") and pager["estimate"]["alert_level"] == "orange"

    # Re-running the pinned fixtures adds nothing.
    before = conn.execute("SELECT count(*) FROM hazard_record_revisions").fetchone()[0]
    assert _run_pack(conn, "latest-again")["status"] == "complete"
    assert conn.execute("SELECT count(*) FROM hazard_record_revisions").fetchone()[0] == before
