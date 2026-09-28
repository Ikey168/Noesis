"""Housing records projected onto places with as-of answers (#1986, U07)."""

from __future__ import annotations

import pytest

from src.kb.geospatial import GeospatialStore
from src.kb.housing import HousingError, forbidden_keys
from src.kb.housing_places import HousingPlaces
from tests.unit import housing_harness as h

NS = h.NS


@pytest.fixture(scope="module")
def env():
    env = h.Env().world()
    env.boris_2100()
    env.bplan_stage_change()
    env.place = env.address()
    yield env
    env.conn.close()


def dossier(env, **kwargs):
    kwargs.setdefault("as_of", "2100-06-01")
    return HousingPlaces(env.conn).dossier(
        NS, principal_id="alice", scopes=h.SCOPES, **kwargs
    )


def test_an_address_gets_its_zone_plan_wohnlage_cells_and_statistics_as_of_a_date(env):
    answer = dossier(env, place_id=env.place["place_id"])
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    land = answer["land_value"]
    (zone,) = land["zones"]
    assert land["status"] == "single_zone" and zone["zone_id"] == "1099001"
    selected = zone["selected"]
    assert (
        selected["valuation_date"],
        selected["value"],
        selected["unit"],
        selected["currency"],
    ) == ("2100-01-01", "5900", "EUR/m²", "EUR")
    assert [r["valuation_date"] for r in zone["prior_revisions"]] == ["2099-01-01"]
    assert (
        zone["selection_basis"]
        == "zone of the source's latest valuation date (edition) not after 2100-06-01"
        and land["receipt_ids"]
    )
    (plan,) = answer["development_plans"]["plans"]
    assert (
        plan["plan_id"],
        plan["stage_on_date"]["stage"],
        plan["stage_on_date"]["stage_date"],
    ) == ("1-99a", "festgesetzt", "2099-06-15")
    assert [s["stage"] for s in plan["stage_history"]] == [
        "aufstellungsbeschluss",
        "festgesetzt",
    ]
    assert [c["category"] for c in answer["residential_area"]["categories"]] == [
        "mittel"
    ]
    (edition,) = answer["rent_index"]["editions"]
    assert (
        edition["edition"]["edition_id"] == "berliner-mietspiegel-2099"
        and edition["wohnlage_edition_matches"]
    )
    assert [c["cell_key"] for c in edition["cells_for_place"]] == ["B1", "B2", "B3"]
    assert "no single cell is chosen" in edition["note"]
    (district,) = answer["district"]["districts"]
    assert (district["name"], district["code"]) == (
        "Mitte",
        "001",
    ) and "gem" in district["match_basis"]
    assert {
        (s["statistic"], s["measure"], s["value"]) for s in district["statistics"]
    } >= {
        ("permits", "dwellings_permitted", "310"),
        ("completions", "dwellings_completed", "260"),
    }
    assert all(
        s["selection_basis"].startswith("the latest vintage")
        for s in district["statistics"]
    )
    (disagreement,) = answer["land"]["disagreements"]
    assert (disagreement["measure"], disagreement["period"]) == (
        "dwellings_permitted",
        "2098",
    )
    assert {r["value"] for r in disagreement["readings"]} == {"1520", "1480"}
    assert "none is chosen" in disagreement["note"]


def test_the_as_of_date_selects_earlier_revisions_stages_and_no_later_edition(env):
    answer = dossier(env, place_id=env.place["place_id"], as_of="2099-02-01")
    (zone,) = answer["land_value"]["zones"]
    assert zone["selected"]["valuation_date"] == "2099-01-01"
    assert [r["valuation_date"] for r in zone["later_revisions"]] == ["2100-01-01"]
    (plan,) = answer["development_plans"]["plans"]
    assert plan["stage_on_date"]["stage"] == "aufstellungsbeschluss"
    assert [s["stage"] for s in plan["later_stages"]] == ["festgesetzt"]
    assert answer["rent_index"]["status"] == "no_edition_valid_on_date"
    assert answer["residential_area"]["status"] == "none"
    assert all(
        s["vintage"] <= "2099-02-01"
        for d in answer["district"]["districts"]
        for s in d["statistics"]
    )
    before = dossier(env, place_id=env.place["place_id"], as_of="2098-06-01")
    assert before["land_value"]["status"] == "outside_published_zones"


def test_a_boundary_point_and_a_point_outside_every_zone_are_reported_never_resolved(
    env,
):
    edge = dossier(env, point=h.wgs84(h.EDGE_UTM))
    assert edge["land_value"]["status"] == "several_zones"
    assert [z["zone_id"] for z in edge["land_value"]["zones"]] == [
        "1099001",
        "1099002",
        "1099003",
    ]
    assert "none is chosen" in edge["land_value"]["note"]
    outside = dossier(env, point=h.wgs84(h.OUTSIDE_UTM))
    assert (
        outside["land_value"]["status"] == "outside_published_zones"
        and outside["land_value"]["zones"] == []
    )
    assert "no value is inferred" in outside["land_value"]["note"]


def test_a_record_whose_geometry_is_missing_is_reported_unchecked_not_outside():
    env = h.Env().world()
    geometry_id = env.store().land_value_history(NS, zone_id="1099001")[0][
        "geometry_id"
    ]
    env.conn.execute(
        "DELETE FROM geospatial_geometries WHERE geometry_id=?", [geometry_id]
    )
    answer = dossier(env, point=h.wgs84(h.ADDRESS_UTM))
    assert answer["land_value"]["status"] == "not_determined"
    (unchecked,) = answer["land_value"]["unchecked_records"]
    assert unchecked["reason"] == "geometry_not_stored"
    env.conn.close()


def test_a_parcel_outline_spanning_two_zones_lists_both(env):
    from src.integrations.spatial import transform_geometry

    ring = [
        [391800.0, 5820400.0],
        [392200.0, 5820400.0],
        [392200.0, 5820600.0],
        [391800.0, 5820600.0],
        [391800.0, 5820400.0],
    ]
    polygon = transform_geometry(
        {"type": "Polygon", "coordinates": [ring]}, "urn:ogc:def:crs:EPSG::25833"
    )["result"]["geometry"]
    parcel = GeospatialStore(env.conn).register_place(
        NS,
        "Flurstück 0999/12 (fiktiv)",
        "parcel",
        names=[{"value": "Flurstück 0999/12", "kind": "canonical"}],
        source_ids={"alkis-flurstueck": "110999-012-00012"},
        parent_ids=[],
        principal_id="alice",
        scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
        geometry=polygon,
    )
    answer = dossier(env, place_id=parcel["place_id"])
    assert answer["input"]["kind"] == "parcel" and "vertices" in answer["input"]["note"]
    assert answer["land_value"]["status"] == "several_zones"
    assert [z["zone_id"] for z in answer["land_value"]["zones"]] == [
        "1099001",
        "1099002",
    ]


def test_a_reviewed_resolution_is_the_address_input_and_an_unreviewed_ambiguous_one_is_not(
    env,
):
    geo = GeospatialStore(env.conn)
    geo_scopes = {
        "knowledge:geospatial:read",
        "knowledge:geospatial:write",
        "knowledge:geospatial:review",
    }
    result = geo.resolve(NS, "Musterstraße 1 (fiktiv)", scopes=geo_scopes)
    saved = geo.save_resolution(result, principal_id="alice", scopes=geo_scopes)
    geo.review(
        NS,
        saved["resolution_id"],
        "accept",
        selected_place_id=env.place["place_id"],
        reason="checked",
        principal_id="bob",
        scopes=geo_scopes,
    )
    answer = dossier(env, resolution_id=saved["resolution_id"])
    assert answer["input"]["resolution"]["basis"] == "accepted review"
    assert answer["land_value"]["zones"][0]["zone_id"] == "1099001"
    geo.register_place(
        NS,
        "Musterstraße 1a (fiktiv)",
        "address",
        names=[{"value": "Musterstraße 1a (fiktiv)"}],
        source_ids={"fixture-address": "1a"},
        parent_ids=[],
        principal_id="alice",
        scopes=geo_scopes,
        geometry={"type": "Point", "coordinates": h.wgs84(h.OUTSIDE_UTM)},
    )
    vague = geo.save_resolution(
        geo.resolve(NS, "Musterstraße", scopes=geo_scopes),
        principal_id="alice",
        scopes=geo_scopes,
    )
    unresolved = dossier(env, resolution_id=vague["resolution_id"])
    assert (
        unresolved["status"] == "place_unresolved"
        and len(unresolved["input"]["resolution"]["candidates"]) == 2
    )


def test_a_district_dossier_lists_statistics_and_plans_but_forms_no_district_value(env):
    answer = dossier(env, district_code="1")
    assert (
        answer["status"] == "answered"
        and answer["land_value"]["status"] == "not_answered_for_districts"
    )
    assert [p["plan_id"] for p in answer["development_plans"]["plans"]] == [
        "1-98",
        "1-99a",
    ]
    (district,) = answer["district"]["districts"]
    assert district["name"] == "Mitte" and len(district["statistics"]) == 4
    assert dossier(env, district_code="13")["status"] == "district_not_found"


def test_receipts_replay_and_change_when_a_source_changes(env):
    places = HousingPlaces(env.conn)
    answer = dossier(env, place_id=env.place["place_id"])
    again = places.replay(answer["receipt"], principal_id="alice", scopes=h.SCOPES)
    assert again["status"] == "reproduced"
    fresh = h.Env().world()
    place = fresh.address()
    first = HousingPlaces(fresh.conn).dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=h.SCOPES,
        place_id=place["place_id"],
    )
    fresh.boris_2100()
    changed = HousingPlaces(fresh.conn).replay(
        first["receipt"], principal_id="alice", scopes=h.SCOPES
    )
    assert changed["status"] == "changed" and changed["added"] and changed["removed"]
    fresh.conn.close()


def test_transit_stops_are_context_only_and_scopes_are_checked(env):
    missing = dossier(env, place_id=env.place["place_id"], include_transit_feed="vbb")
    assert missing["transit_context"]["status"] == "transit_feed_not_acquired"
    env.run(["vbb-gtfs"], "vbb-feed", operation="feed")
    answer = dossier(env, place_id=env.place["place_id"], include_transit_feed="vbb")
    assert "no valuation meaning" in answer["transit_context"]["note"]
    assert (
        isinstance(answer["transit_context"]["stops"], list)
        and answer["transit_context"]["feed_version"]
    )
    assert (
        answer["land_value"]
        == dossier(env, place_id=env.place["place_id"])["land_value"]
    )
    with pytest.raises(HousingError) as denied:
        HousingPlaces(env.conn).dossier(
            NS,
            as_of="2100-06-01",
            principal_id="alice",
            scopes={h.READ, f"namespace:{NS}:read"},
            place_id=env.place["place_id"],
        )
    assert denied.value.code == "unauthorized"
    with pytest.raises(HousingError):
        dossier(env, place_id=env.place["place_id"], point=[13.4, 52.5])


def test_zone_comparison_lists_valuation_dates_and_sources_side_by_side(env):
    compared = HousingPlaces(env.conn).compare_zone(
        NS, zone_id="1099001", scopes=h.SCOPES
    )
    assert [d["valuation_date"] for d in compared["valuation_dates"]] == [
        "2099-01-01",
        "2100-01-01",
    ]
    assert [
        [r["value"] for r in d["readings"]] for d in compared["valuation_dates"]
    ] == [["5400"], ["5900"]]
    assert not any(d["sources_disagree"] for d in compared["valuation_dates"])
    assert (
        "no change, trend or interpolated value" in compared["note"]
        and forbidden_keys(compared) == []
    )


def test_a_dossier_built_against_another_geospatial_namespace_replays_with_its_parameters():
    env = h.Env().world()
    place = GeospatialStore(env.conn).register_place(
        "stadtplan",
        "Musterstraße 1 (fiktiv)",
        "address",
        names=[{"value": "Musterstraße 1 (fiktiv)", "kind": "canonical"}],
        source_ids={"fixture-address": "stadtplan-1"},
        parent_ids=[],
        principal_id="alice",
        scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
        geometry={"type": "Point", "coordinates": h.wgs84(h.ADDRESS_UTM)},
    )
    places = HousingPlaces(env.conn)
    answer = places.dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=h.SCOPES,
        geo_namespace="stadtplan",
        place_id=place["place_id"],
    )
    assert (
        answer["status"] == "answered"
        and answer["land_value"]["zones"][0]["zone_id"] == "1099001"
    )
    assert answer["receipt"]["request"]["parameters"]["geo_namespace"] == "stadtplan"
    again = places.replay(answer["receipt"], principal_id="alice", scopes=h.SCOPES)
    assert again["status"] == "reproduced"
    env.conn.close()


def test_a_renumbered_or_dropped_zone_of_an_earlier_stichtag_is_not_current():
    from tests.unit import housing_fixture_builder as fb

    env = h.Env().world()
    features = fb.boris_features("2100-01-01")
    features[0]["properties"]["wnum"] = (
        "1100001"  # zone A renumbered in the 2100 edition
    )
    del features[2]  # zone C no longer published
    adapter = env.wfs_adapter(
        "berlin-boris-bodenrichtwerte", features, "2100-03-01T08:00:00Z"
    )
    env.run(
        ["berlin-boris-bodenrichtwerte"],
        "boris-2100-renumbered",
        adapters={"berlin-boris-bodenrichtwerte": adapter},
    )
    answer = dossier(env, point=h.wgs84(h.ADDRESS_UTM))
    assert answer["land_value"]["status"] == "single_zone"
    (zone,) = answer["land_value"]["zones"]
    assert (zone["zone_id"], zone["selected"]["valuation_date"]) == (
        "1100001",
        "2100-01-01",
    )
    edge = dossier(env, point=h.wgs84(h.EDGE_UTM))
    assert [z["zone_id"] for z in edge["land_value"]["zones"]] == ["1099002", "1100001"]
    earlier = dossier(env, point=h.wgs84(h.ADDRESS_UTM), as_of="2099-06-01")
    assert [z["zone_id"] for z in earlier["land_value"]["zones"]] == ["1099001"]
    env.conn.close()


def test_a_suppressed_statistics_figure_is_absent_not_a_disagreement():
    from src.ingestion.housing_sources import parse_publication
    from tests.unit import housing_fixture_builder as fb

    env = h.Env().world()
    document = fb.statbb_document("permits")
    csv = fb.statbb_csv("permits").replace(
        "00;Berlin;2098;1520;98,4", "00;Berlin;2098;.;x"
    )
    parsed = parse_publication(
        "statbb-building-csv",
        csv.encode(),
        document=document,
        headers={"Last-Modified": fb.last_modified("2099-09-30")},
    )
    header = {k: parsed[k] for k in parsed if k != "items"} | {
        "document": document,
        "evidence_origin": "fixture",
    }
    env.store().apply_publication(
        NS, header, parsed["items"], source_id="statistik-bb-bautaetigkeit"
    )
    answer = dossier(env, point=h.wgs84(h.ADDRESS_UTM))
    land = {(s["measure"], s["period"]): s for s in answer["land"]["statistics"]}
    assert land[("dwellings_permitted", "2098")]["value_text"] == "."
    assert answer["land"]["disagreements"] == []
    env.conn.close()
