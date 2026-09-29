"""NH10-NH11 (#2351, #2355): events affecting a place as of a date; advisories and alerts in force at a time."""

from __future__ import annotations

import pytest

from src.kb.hazards_identity import HazardIdentity
from src.kb.hazards_queries import NONE_ON_RECORD, NOT_COVERED, HazardQueryError, alerts_in_force, events_affecting
from tests.unit.hazards import harness as h
from tests.unit.hazards.test_hazards_identity_links import SAMOS, by_pair

BAHAMAS = {"type": "Polygon", "coordinates": [[[-79.5, 25.5], [-77.0, 25.5], [-77.0, 27.5], [-79.5, 27.5], [-79.5, 25.5]]]}
ALGARVE = {"type": "Polygon", "coordinates": [[[-9.0, 36.9], [-7.4, 36.9], [-7.4, 37.5], [-9.0, 37.5], [-9.0, 36.9]]]}
KARLOVASI = [26.70, 37.79]


def place(store, name, geometry, iso3):
    record = store.geo.register_place(h.NS, name, "region", names=[{"value": name, "language": "en", "kind": "canonical"}],
                                      source_ids={"iso3": iso3}, parent_ids=[], principal_id="operator", scopes=h.SCOPES,
                                      place_key=f"authored:{name}")
    store.geo.store_geometry(h.NS, geometry, place_id=record["place_id"], crs="EPSG:4326", precision_m=0.0,
                             simplified_from=None, disputed=False, admin_hierarchy=[],
                             source={"kind": "authored-boundary"}, evidence=[], principal_id="operator", scopes=h.SCOPES,
                             generation=1, valid_from_ms=h.ms("2098-01-01T00:00:00Z"))
    return record["place_id"]


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.load_all(conn)
    identity = HazardIdentity(conn, now=lambda: h.ms("2099-09-03T00:00:00Z"))
    identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    pair = by_pair(identity, identity.store)[(frozenset({"us7000zz01", "20990810_0000031"}), "proximity")]
    identity.review(h.NS, pair["correspondence_id"], "accept", "same origin", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    places = {"samos": place(identity.store, "Samos", SAMOS, "GRC"),
              "bahamas": place(identity.store, "Northwestern Bahamas", BAHAMAS, "BHS"),
              "algarve": place(identity.store, "Algarve", ALGARVE, "PRT")}
    return conn, places


def ask(conn, **kwargs):
    return events_affecting(conn, h.NS, scopes=h.SCOPES, principal_id="analyst", **kwargs)


def test_as_of_selects_the_revision_then_in_force_and_lists_the_full_history(world):
    conn, _ = world
    early = ask(conn, point=KARLOVASI, radius_m=50_000, start="2099-08-01", end="2099-08-31",
                as_of="2099-08-10T05:00:00Z")
    by = {e["native_id"]: e for e in early["events"]}
    assert set(by) == {"us7000zz01", "20990810_0000031", "EQ1400001"}  # the fire was not yet published
    usgs = by["us7000zz01"]
    assert usgs["status"] == "automatic" and {p["value"] for p in usgs["parameters"]} == {"5.8", "10"}
    assert usgs["revision_used"]["revision_key"] == str(h.ms("2099-08-10T03:20:00Z")) and usgs["revision_used"]["later_revisions_exist"]
    assert [r["known_at_as_of"] for r in usgs["revision_history"]] == [True, False]
    magnitude = next(c for c in usgs["revision_history"][1]["changes"] if c["parameter"] == "magnitude")
    assert magnitude["before"] == {"value": "5.8", "unit": "magnitude", "qualifier": "mb"}
    assert magnitude["after"] == {"value": "6.1", "unit": "magnitude", "qualifier": "mww"}
    assert usgs["revision_history"][1]["source_url"].startswith("https://earthquake.usgs.gov/")
    (emsc,) = usgs["correspondents"]
    assert emsc["provider"] == "emsc" and emsc["note"] == "shown beside, never merged or averaged"
    assert {p["name"]: p["value"] for p in emsc["parameters"]}["magnitude"] == "5.9"  # EMSC's own, as of the same date
    assert usgs["matched_by"]["spatial_receipt_id"] and "not an impact footprint" in usgs["matched_by"]["relation"]
    assert usgs["citation"]["issuing_body"].startswith("U.S. Geological Survey")
    latest = ask(conn, point=KARLOVASI, radius_m=50_000, start="2099-08-01", end="2099-08-31")
    now = {e["native_id"]: e for e in latest["events"]}
    assert now["us7000zz01"]["status"] == "reviewed" and "99001" in now and "WF1020001" in now
    assert early["exclusions"] == latest["exclusions"] and "risk scores" in early["exclusions"]


def test_place_boundary_none_on_record_and_source_not_covered(world):
    conn, places = world
    samos = ask(conn, place_id=places["samos"], start="2099-08-01", end="2099-08-31", as_of="2099-08-14")
    fire = next(e for e in samos["events"] if e["native_id"] == "99001")
    assert {p["name"]: p["value"] for p in fire["parameters"]}["burnt_area"] == "120"
    assert fire["matched_by"]["boundary"]["generation"] == 1
    assert {e["native_id"] for e in samos["events"]} == {"99001", "WF1020001"}  # the offshore epicentre is outside
    algarve = ask(conn, place_id=places["algarve"], start="2099-08-01", end="2099-08-31")
    assert algarve["events"] == [] and algarve["answer"] == NONE_ON_RECORD and algarve["coverage"]["effis"] == "covered"
    outback = ask(conn, bbox=[130, -30, 140, -20], start="2099-08-01", end="2099-08-31")
    assert outback["answer"] == NOT_COVERED
    with pytest.raises(HazardQueryError) as caught:
        ask(conn, point=KARLOVASI, radius_m=50_000, start="2098-01-01", end="2099-08-31")
    assert caught.value.code == "unbounded_window"
    with pytest.raises(HazardQueryError) as caught:
        ask(conn, point=KARLOVASI, radius_m=5_000_000, start="2099-08-01", end="2099-08-31")
    assert caught.value.code == "unbounded_area"


def test_alerts_in_force_quote_level_and_wording_with_the_supersession_chain(world):
    conn, places = world

    def alerts(**kwargs):
        return alerts_in_force(conn, h.NS, scopes=h.SCOPES, principal_id="analyst", **kwargs)

    ten = alerts(place_id=places["bahamas"], at="2099-09-01T10:00:00Z")
    nhc = [a for a in ten["alerts"] if a["provider"] == "nhc"]
    assert [a["advisory_number"] for a in nhc] == ["12"] and nhc[0]["superseded_numbers"] == ["11"]
    assert nhc[0]["matched_by"]["quoted_area"] == "THE NORTHWESTERN BAHAMAS"
    assert nhc[0]["validity"]["window"] == ["2099-09-01T09:00:00Z", "2099-09-01T12:00:00Z"]
    gdacs = [a for a in ten["alerts"] if a["provider"] == "gdacs"]
    assert gdacs[0]["level"] == "Red" and gdacs[0]["matched_by"]["country"] == "BHS"
    thirteen = alerts(place_id=places["bahamas"], at="2099-09-01T13:00:00Z")
    (intermediate,) = [a for a in thirteen["alerts"] if a["provider"] == "nhc"]
    assert intermediate["advisory_number"] == "12A" and intermediate["superseded_numbers"] == ["11", "12"]
    then = alerts(place_id=places["bahamas"], at="2099-09-01T10:00:00Z", as_of="2099-09-01T10:00:00Z")
    (known,) = [a for a in then["alerts"] if a["provider"] == "nhc"]
    assert known["validity"]["basis"] == "open (no published expiry)"  # 12A was not yet issued at that moment
    early = alerts(place_id=places["bahamas"], at="2099-09-01T02:00:00Z")
    assert early["alerts"] == [] and early["per_provider"]["nhc"] == "no alert on record"

    samos = alerts(place_id=places["samos"], at="2099-08-10T06:00:00Z")
    (quake,) = [a for a in samos["alerts"] if a["provider"] == "gdacs"]
    assert (quake["episode_id"], quake["level"]) == ("1500001", "Orange")
    assert quake["wording"] == "Magnitude 5.8M, Depth:10km" and quake["validity"]["window"] == [
        "2099-08-10T04:00:00Z", "2099-08-10T10:00:00Z"]
    later = alerts(place_id=places["samos"], at="2099-08-10T11:00:00Z")
    (quake,) = [a for a in later["alerts"] if a["provider"] == "gdacs" and a["title"].startswith("GDACS Orange alert: Earth")]
    assert quake["episode_id"] == "1500002" and quake["superseded_numbers"] == ["1500001"]
    assert quake["citation"]["revision_key"] == "episode:1500002"

    evros = alerts(point=[26.3, 41.6], radius_m=10_000, country="GRC", at="2099-08-22T00:00:00Z")
    (flood,) = [a for a in evros["alerts"] if a["provider"] == "glofas"]
    assert flood["modelled"] and flood["model"]["version"] == "4.0" and flood["thresholds"][0]["return_period_years"] == "5"
    expired = alerts(point=[26.3, 41.6], radius_m=10_000, country="GRC", at="2099-08-26T00:00:00Z")
    assert not [a for a in expired["alerts"] if a["provider"] == "glofas"]
    outback = alerts(point=[135.0, -25.0], radius_m=10_000, at="2099-08-22T00:00:00Z")
    assert outback["answer"] == NOT_COVERED and set(outback["per_provider"].values()) == {"source not covered"}
    assert "not a warning" in outback["notice"]
