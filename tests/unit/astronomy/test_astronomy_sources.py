"""Astronomy acquisition (#2149, AS03-AS06): MPC/JPL, Exoplanet Archive, GCAT/SATCAT and SWPC through the adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion.astronomy_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    AstronomyAdapter,
    fixture_transport,
    parse_swpc_message,
)
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    load_source_packs,
)
from src.kb.astronomy_store import AstronomyStore
from tests.unit.astronomy import harness as h

ROOT = Path(__file__).resolve().parents[3]


def views(conn, kind, **filters):
    store = AstronomyStore(conn)
    out = []
    for view in store.visible(h.NS, kinds=[kind])["records"]:
        if all(view["record"].get(k) == v for k, v in filters.items()):
            out.append(view)
    return out


def one(conn, kind, **filters):
    found = views(conn, kind, **filters)
    assert len(found) == 1, (kind, filters, [v["record"] for v in found])
    return found[0]


def test_production_pack_validates_and_replays_its_pinned_fixtures():
    packs = {p["pack_id"]: p for p in load_source_packs(ROOT / "config/source_packs")}
    pack = packs["astronomy-and-space"]
    # 14 astronomy sources plus the space-object registration feature's 5 (#2224).
    assert pack["domains"] == ["astronomy"] and len(pack["sources"]) == 19
    assert {s["connector"] for s in pack["sources"]} == {"astronomy", "astronomy-registration"}
    assert sum(s["connector"] == "astronomy" for s in pack["sources"]) == 14
    report = SourcePackConformance(ROOT).offline(
        json.loads((ROOT / "config/source_packs/astronomy.json").read_text())
    )
    assert report["valid"], [s for s in report["sources"] if not s["valid"]]


def test_access_decisions_are_recorded_and_nothing_is_live_verified():
    assert PROVIDER_CONTRACTS["space-track"]["access_decision"] == "not-implemented"
    assert PROVIDER_CONTRACTS["jpl-horizons"]["access_decision"] == "link-only"
    assert PROVIDER_CONTRACTS["esa-neocc"]["access_decision"] == "not-implemented"
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {
        "unverified-live",
        "link-only",
        "not-implemented",
    }


def test_mpc_identification_appends_revisions_and_a_linked_designation():
    conn = h.connection()
    receipts = h.acquire(conn, "identifier", "2099-01-20")
    assert (
        receipts[0]["counts"]["out_of_scope"] == 1
    )  # 2099 YZ1 is outside the declared bound, counted
    first = one(conn, "designation", designation="2099 AB12")
    assert "object_designation" not in first["record"]
    h.acquire(conn, "identifier", "2099-04-10")
    revised = one(conn, "designation", designation="2099 AB12")
    assert (
        revised["record"]["object_designation"] == "2098 QX7"
        and revised["revision"] == 2
    )
    identification = one(conn, "identification", designation="2099 AB12")["record"]
    assert (
        identification["identified_with"] == "2098 QX7"
        and identification["permanent"] == "(999901)"
    )
    assert (
        identification["announced_in"] == "MPEC 2099-G42"
        and identification["announced_on"] == "2099-04-08"
    )
    numbered = one(conn, "small_body", primary_designation="2098 QX7")["record"]
    assert numbered["number"] == 999901 and numbered["name"] == "Fictaria"
    assert numbered["stated_designations"] == ["2099 AB12"]
    # Re-acquiring the same response adds nothing.
    before = AstronomyStore(conn).generation(h.NS)
    h.acquire(
        conn,
        "identifier",
        "2099-04-10",
        run_id="again",
        observed_at_ms=h.ms("2099-04-11"),
    )
    assert AstronomyStore(conn).generation(h.NS) == before


def test_mpc_and_jpl_orbit_solutions_are_separate_vintages_side_by_side():
    conn = h.connection()
    for stage, key in h.steps():
        if key in {"mpcorb", "sbdb"}:
            h.acquire(conn, key, stage)
    solutions = [v["record"] for v in views(conn, "orbit_solution")]
    assert sorted((s["publisher"], s["solution_id"]) for s in solutions) == [
        ("JPL", "12"),
        ("JPL", "3"),
        ("MPC", "E2099-B17"),
        ("MPC", "MPO999123"),
    ]
    mpc = next(s for s in solutions if s["solution_id"] == "E2099-B17")
    assert mpc["epoch"] == {
        "jd": "2487737.5",
        "calendar": "2099-02-03",
        "scale": "TT",
        "stated": "K9923",
    }
    assert mpc["elements"]["a"] == {"value": "2.2", "unit": "au"} and mpc[
        "uncertainty"
    ] == {"parameter": "U", "value": "6"}
    assert mpc["n_obs_used"] == 24 and mpc["arc"] == {
        "stated": "14 days",
        "days": "14",
        "last_obs": "2099-01-31",
    }
    jpl = next(s for s in solutions if s["solution_id"] == "12")
    assert (
        jpl["object_designation"] == "(999901)"
        and jpl["computed_at"] == "2099-05-04T09:30:00Z"
    )
    assert jpl["arc"] == {
        "first_obs": "2098-08-21",
        "last_obs": "2099-04-25",
        "days": "247",
    }
    assert jpl["uncertainty"] == {"parameter": "condition_code", "value": "2"}
    assert jpl["elements"]["e"] == {"value": "0.2344011", "sigma": "0.00002"}
    # The source-stated designation links the JPL numbered object to the provisional one.
    body = one(conn, "small_body", primary_designation="(999901)")["record"]
    assert (
        body["stated_designations"] == ["2098 QX7", "2099 AB12"]
        and body["spk_id"] == "20999901"
    )


def test_sentry_listing_is_quoted_and_its_removal_appends_a_revision():
    conn = h.connection()
    h.acquire(conn, "sentry", "2099-02-06")
    listed = one(conn, "impact_risk_listing")["record"]
    assert listed["listing_status"] == "listed" and listed["figures"]["ip"] == "3.1e-07"
    assert (
        listed["figures"]["ps_cum"] == "-5.20"
        and listed["listing_date"] == "2099-02-05T12:00:00Z"
    )
    h.acquire(conn, "sentry", "2099-04-12")
    removed = one(conn, "impact_risk_listing")
    assert removed["record"]["listing_status"] == "removed" and removed["record"][
        "removed_at"
    ] == ("2099-04-11T08:00:00Z")
    assert removed["revision"] == 2 and "figures" not in removed["record"]


def test_exoplanet_dispositions_per_table_with_status_history_and_retraction_as_stated():
    conn = h.connection()
    for stage, key in h.steps():
        if key in {"ps", "pscomppars", "toi", "koi", "removed"}:
            h.acquire(conn, key, stage)
    store = AstronomyStore(conn)
    toi = one(conn, "exoplanet_status_assertion", object_name="99902.01")
    assert (
        toi["record"]["disposition"] == "false_positive"
        and toi["record"]["native_disposition"] == "FP"
    )
    assert [
        r["record"]["disposition"] for r in store.history(h.NS, toi["record_id"])
    ] == ["candidate", "false_positive"]
    promoted = one(conn, "exoplanet_status_assertion", object_name="99901.01")["record"]
    assert (
        promoted["disposition"] == "confirmed"
        and promoted["native_disposition"] == "CP"
    )
    # Parameters are kept per reference; the composite set is labelled the archive's composite.
    sets = views(conn, "exoplanet", name="Fict-101 b")
    assert sorted(
        (v["record"]["source_table"], v["record"]["composite"]) for v in sets
    ) == [("ps", False), ("ps", False), ("pscomppars", True)]
    default = next(v["record"] for v in sets if v["record"].get("default_set"))
    assert default["reference"]["bibcode"] == "2099AJ....999..101F"
    assert (
        default["identifiers"]["toi"] == "99901.01"
        and default["parameters"]["pl_rade"]["unit"] == "R_earth"
    )
    assert default["discovery"]["reference"]["doi"] == "10.5555/fict.2099.101"
    # The retracted planet left the complete ps listing (observed, never deleted) and the removed listing says why.
    confirmed = one(
        conn, "exoplanet_status_assertion", object_name="Fict-303 c", source_table="ps"
    )
    assert confirmed["record"]["disposition"] == "confirmed"
    assert (
        confirmed["listing"]["state"] == "no_longer_listed"
        and confirmed["listing"]["observed_on"] == "2099-06-01"
    )
    retracted = one(
        conn,
        "exoplanet_status_assertion",
        object_name="Fict-303 c",
        source_table="removed",
    )["record"]
    assert (
        retracted["disposition"] == "retracted"
        and retracted["asserted_at"] == "2099-05-28"
    )
    assert retracted["reference"]["doi"] == "10.5555/fict.2099.9903"
    koi = one(conn, "exoplanet_status_assertion", object_name="K99903.01")["record"]
    assert (
        koi["identifiers"] == {"kepid": "99900303", "pl_name": "Fict-303 b"}
        and koi["host"] == "Fict-303"
    )


def test_launches_outcomes_objects_and_disagreements_are_kept_side_by_side():
    conn = h.connection()
    for stage, key in h.steps():
        if key in {"gcat_launch", "gcat_satcat", "gcat_orgs", "satcat"}:
            h.acquire(conn, key, stage)
    outcomes = {
        v["record"]["launch_tag"]: v["record"] for v in views(conn, "launch_outcome")
    }
    assert {t: (o.get("outcome"), o["native_code"]) for t, o in outcomes.items()} == {
        "2099-001": ("success", "OS"),
        "2099-002": ("failure", "OF"),
        "2099-003": ("partial", "OP"),
    }
    launch = one(conn, "launch", launch_tag="2099-001")["record"]
    assert (
        launch["time"] == "2099-03-03T14:15:00Z"
        and launch["stated_time"] == "2099 Mar 3 1415:00"
    )
    assert launch["agency_codes"] == ["FICTSPACE"] and launch["site_code"] == "FKSC"
    gcat = one(conn, "orbital_object", jcat="S99901")
    assert (
        gcat["record"]["decay_date"] == "2099-06-20"
        and gcat["record"]["stated_decay_date"] == "2099 Jun 20?"
    )
    assert gcat["revision"] == 2
    satcat = one(conn, "orbital_object", norad="99901", owner="FICT")
    assert (
        satcat["record"]["decay_date"] == "2099-06-21"
        and satcat["record"]["status"] == "D"
    )
    assert satcat["revision"] == 2
    # No orbital element set is read from SATCAT.
    assert not {"PERIOD", "period", "inclination", "apogee"} & set(satcat["record"])
    site = one(conn, "space_organisation", code="FKSC")["record"]
    assert site["latitude"] == "-10" and site["longitude"] == "-30"


def test_swpc_products_accumulate_beyond_the_rolling_feed_with_threaded_references():
    conn = h.connection()
    h.acquire(conn, "swpc", "2099-09-01T13")
    h.acquire(conn, "swpc", "2099-09-02T07")
    products = {
        v["record"]["serial"]: v["record"] for v in views(conn, "space_weather_product")
    }
    assert sorted(products) == [
        "9001",
        "9002",
        "9003",
        "9004",
    ]  # 9001 left the feed and stays recorded
    assert products["9001"]["product_kind"] == "watch" and products["9001"][
        "scales"
    ] == ["G2"]
    assert products["9003"]["product_kind"] == "extended_warning"
    assert products["9003"]["references"] == {"extends": "9002"}
    assert products["9004"]["product_kind"] == "cancel_watch" and products["9004"][
        "references"
    ] == {"cancels": "9001"}
    assert products["9002"]["issue_time"] == "2099-09-01T12:00:00Z"
    assert "Fictional authored message" in products["9002"]["message"]  # verbatim


def test_swpc_header_parsing_reads_only_structured_fields():
    header = parse_swpc_message(
        "Space Weather Message Code: ALTK06\r\nSerial Number: 7\r\nIssue Time: 2099 Sep 03 "
        "0100 UTC\r\n\r\nALERT: Geomagnetic K-index of 6\r\nThreshold Reached: ...\r\n"
        "Storm levels like G3 are mentioned in prose only"
    )
    assert (
        header["product_kind"] == "alert"
        and header["scales"] is None
        and header["serial"] == "7"
    )


def test_rows_never_inherit_values_and_bad_rows_are_rejections():
    source = h.fictional("toi", ["bad.csv"])
    body = "toi,tid,tfopwg_disp,rowupdate\n99901.01,999000101,PC,2099-01-10\n99902.01,999000202,,2099-01-12\n"
    adapter = AstronomyAdapter(
        source,
        transport=fixture_transport([{"request": "/fixture/bad.csv", "body": body}]),
    )
    page = adapter.fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    records = [r for r in page.records if r.get("astronomy_record")]
    rejected = [r for r in page.records if r.get("rejection")]
    assert (
        len(records) == 1 and len(rejected) == 1
    )  # the empty disposition is not taken from the row above


def test_a_complete_listing_with_an_unidentified_rejection_is_not_complete():
    source = h.fictional("ps", ["ps.csv"])
    body = (
        h.FIXTURES / "ps_2099-06-01.csv"
    ).read_text() + ",Fict-101,1,,,,,,0,,,,,,,,,,,\n"
    adapter = AstronomyAdapter(
        source,
        transport=fixture_transport([{"request": "/fixture/ps.csv", "body": body}]),
    )
    page = adapter.fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    assert (
        page.receipt["listing"]["complete"] is False
        and "incomplete_reason" in page.receipt["listing"]
    )


def test_adapter_refuses_ad_hoc_queries_other_hosts_and_truncation():
    source = h.fictional("swpc", ["swpc_alerts_2099-09-01T13.json"])
    pages = h.pages(["swpc_alerts_2099-09-01T13.json"])
    adapter = AstronomyAdapter(source, transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as error:
        adapter.fetch_page(
            {"operation": "documents", "parameters": {"q": "x"}}, cursor=None
        )
    assert error.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as error:
        adapter.fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 1}, cursor=None
        )
    assert error.value.code == "budget_exhausted"
    moved = [{**pages[0], "final_url": "https://elsewhere.example/alerts.json"}]
    with pytest.raises(SourcePackError) as error:
        AstronomyAdapter(source, transport=fixture_transport(moved)).fetch_page(
            {"operation": "documents", "parameters": {}}, cursor=None
        )
    assert error.value.code == "network_policy"
    bad = json.loads(json.dumps(source))
    bad["astronomy"]["documents"][0]["listing"] = "complete"
    with pytest.raises(SourcePackError):
        AstronomyAdapter(h._rehash(bad), transport=fixture_transport(pages))
