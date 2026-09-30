"""Offline acceptance: a place to cited capacity indicators with definitions and vintages (#2215, HS11).

The pinned ``clinical-evidence`` health-capacity fixtures (WHO GHO OData, OECD
Health Statistics SDMX-CSV, Eurostat SDMX-CSV) replay through the real
source-pack runtime, the health-capacity connector (the surveillance adapter
with the SDMX connector), the surveillance projector and store, Geospatial place
resolution, the alignment records, the as-of query and a monitor. No request
leaves the process; receipts say ``fixture``. Live coverage is HS12 (#2481).
"""

from __future__ import annotations

import pytest

from src.kb.health_capacity import NEVER_SENTENCE
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.health_capacity_links import HealthCapacityLinks
from src.kb.health_capacity_monitoring import HealthCapacityMonitor
from src.kb.health_capacity_queries import capacity_as_of
from tests.unit.clinical import health_capacity_harness as h

OECD_BEDS = {"provider": "oecd-health", "source_code": "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS"}
ESTAT_BEDS = {"provider": "eurostat-health", "source_code": "hlth_rs_bds1:HBEDT"}
GHO_BEDS = {"provider": "who-gho", "source_code": "WHS6_102"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_place_to_cited_capacity_indicators_with_definitions_notes_breaks_and_vintages():
    env = h.Env()
    first = env.acquire("r1")
    assert first["status"] == "complete"
    rows = env.conn.execute(
        "SELECT DISTINCT provider, evidence_origin FROM surveillance_releases ORDER BY 1").fetchall()
    assert rows == [("eurostat-health", "fixture"), ("oecd-health", "fixture"), ("who-gho", "fixture")]

    # Place resolution: codes to Geospatial places by the published code; aggregates stay aggregates.
    places = h.register_places(env.conn)
    tool = HealthCapacityComparability(env.conn, now=env.clock)
    resolved = tool.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert len(resolved["matched"]) == 4 and len(resolved["aggregate"]) == 3

    # Alignment: an equivalent mapping and a definition-difference note, both definitions cited.
    mapping = tool.propose_mapping(h.NS, GHO_BEDS, ESTAT_BEDS, "equivalent", "staffed hospital beds in both",
                                   principal_id="analyst", scopes=h.SCOPES)
    tool.review_mapping(h.NS, mapping["mapping_id"], "accept", "definitions cited", principal_id="reviewer",
                        scopes=h.SCOPES)
    note = tool.record_note(h.NS, OECD_BEDS, ESTAT_BEDS, "same_concept_different_definition",
                            "OECD excludes day-care beds for France from 2097 (country note); units differ",
                            principal_id="analyst", scopes=h.SCOPES)
    tool.review_note(h.NS, note["note_id"], "accept", "cited", principal_id="reviewer", scopes=h.SCOPES)

    # The journey: Germany as of mid-2098, every source per domain, cited.
    answer = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=places["DEU"], as_of="2098-06-30")
    assert answer["status"] == "answered" and answer["boundary"] == NEVER_SENTENCE
    assert {c["code"] for c in answer["place"]["codes"]} == {"DEU", "DE"}
    assert {d: set(p) for d, p in answer["domains"].items()} == {
        "beds": {"who-gho", "oecd-health", "eurostat-health"},
        "workforce": {"who-gho", "oecd-health"},
        "expenditure": {"who-gho", "eurostat-health"},
    }
    # OECD 1.0 was released after the as-of date (Last-Modified July 2098): listed, unavailable, never estimated.
    (oecd_then,) = answer["domains"]["beds"]["oecd-health"]
    assert oecd_then["status"] == "unavailable" and oecd_then["values"] == []
    (gho_beds,) = answer["domains"]["beds"]["who-gho"]
    citation = gho_beds["citation"]
    assert citation["source_revision"]["url"].startswith("https://ghoapi.azureedge.net/api/")
    assert {"kind": "gho-indicator", "identifier": "WHS6_102"} in citation["identifiers"]
    assert gho_beds["definitions"][0]["text"] and gho_beds["unit"] == "per 10 000 population"
    assert [m["mapping_id"] for m in gho_beds["mappings"]] == [mapping["mapping_id"]]
    (eurostat_beds,) = answer["domains"]["beds"]["eurostat-health"]
    assert [n["note_id"] for n in eurostat_beds["comparability_notes"]] == [note["note_id"]]
    assert eurostat_beds["citation"]["source_revision"]["native_revision"].startswith("hlth_rs_bds1@")
    assert eurostat_beds["values"][-1]["flags"] == ["p: provisional"]
    hf1 = next(e for e in answer["domains"]["expenditure"]["eurostat-health"] if e["source_code"].endswith("HF1"))
    assert {b["capacity_kind"] for b in hf1["breaks"]} >= {"definition-break"}
    assert [v["definition"]["version"] for v in hf1["values"]] == ["SHA 1.0 HF.1", "SHA 2011 HF.1"]
    (gho_expenditure,) = answer["domains"]["expenditure"]["who-gho"]
    assert gho_expenditure["values"][-1]["status"] == "unknown"

    # A later Eurostat update: a later as-of date sees it (and the OECD release), an earlier one still does not.
    env.eurostat_update("hlth_rs_bds1")
    second = env.acquire("r2")
    assert second["status"] == "complete"
    later = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=places["DEU"], as_of="2098-12-31",
                           domains=["beds"])
    assert set(later["domains"]["beds"]) == {"who-gho", "oecd-health", "eurostat-health"}
    (oecd,) = later["domains"]["beds"]["oecd-health"]
    assert oecd["indicator_notes"]["country_note"] and oecd["citation"]["source_revision"]["native_revision"] \
        == "OECD.ELS.HD,DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC,1.0"
    (revised,) = later["domains"]["beds"]["eurostat-health"]
    assert revised["values"][-1]["value"] == "783.4"
    assert revised["vintage_differences"]["left"]["vintage_id"] == eurostat_beds["vintage"]["vintage_id"]
    again = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=places["DEU"], as_of="2098-06-30",
                           domains=["beds"])
    assert again["domains"]["beds"]["eurostat-health"][0]["values"][-1]["value"] == "782.0"

    # Beside surveillance and cited denominators.
    beside = HealthCapacityLinks(env.conn, now=env.clock).beside_surveillance(h.NS, places["DEU"],
                                                                               scopes=h.READ_ONLY)
    assert beside["capacity"] and beside["status"] == "resolved"

    # A place with no data is reported as having none on record.
    none = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_code="ITA")
    assert none["status"] == "none_on_record" and none["domains"] == {}

    # A monitor on the place delivers the Eurostat revision once, with both vintages cited.
    monitor = HealthCapacityMonitor(env.conn, now=env.clock)
    created = monitor.create(h.NS, "acceptance", watch={"place_code": "DE", "indicators": ["hlth_rs_bds1:HBEDT"]},
                             principal_id="alice", scopes=h.SCOPES)
    ran = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    assert sorted(n["kind"] for n in ran["notifications"]) == ["new-release", "revision"]
    assert monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Re-acquisition of unchanged publications adds nothing.
    counts = env.conn.execute("SELECT count(*) FROM surveillance_vintages").fetchone()[0]
    env.acquire("r3")
    assert env.conn.execute("SELECT count(*) FROM surveillance_vintages").fetchone()[0] == counts
