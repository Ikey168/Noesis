"""Constituency boundaries projected into the Geospatial store; place-based results as of a date (#1965)."""

from __future__ import annotations

import pytest

from src.integrations.spatial import transform_geometry
from src.kb.elections import ElectionError, forbidden_keys
from src.kb.elections_geo import ElectionGeography, collection_name
from src.kb.geospatial import GeospatialStore
from src.kb.geospatial_features import GeospatialFeatureStore
from tests.unit import elections_harness as h

GEO = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
    "knowledge:geospatial:review",
}
SCOPES = h.SCOPES | GEO


def square(west, east, south=5_800_000, north=5_810_000):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def boundaries(splits):
    """Wahlkreis squares in ETRS89 / UTM 32N (fictional areas), ids as published (unpadded numbers)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"WKR_NR": number, "WKR_NAME": name},
                "geometry": {"type": "Polygon", "coordinates": square(west, east)},
            }
            for number, name, west, east in splits
        ],
    }


VINTAGE_2025 = boundaries(
    [(1, "Musterstadt", 500_000, 510_000), (2, "Beispielkreis", 510_000, 520_000)]
)
# In the 2103 vintage the boundary moved west: the strip 505-510 km now belongs to Wahlkreis 2.
VINTAGE_2103 = boundaries(
    [(1, "Musterstadt", 500_000, 505_000), (2, "Beispielkreis", 505_000, 520_000)]
)


def wgs84(easting, northing=5_805_000):
    return transform_geometry(
        {"type": "Point", "coordinates": [easting, northing]}, "EPSG:25832"
    )["result"]["geometry"]["coordinates"]


@pytest.fixture()
def geo():
    conn = h.connection()
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    h.apply(conn, "de-btw", h.DE_FINAL)
    h.apply_next_federal(conn)
    geography = ElectionGeography(conn, now=h.Clock())
    geography.project_boundaries(
        "global",
        scheme="de-bt-wahlkreis",
        boundary_vintage="btw2025",
        feature_collection=VINTAGE_2025,
        id_property="WKR_NR",
        title_property="WKR_NAME",
        valid_from="2023-01-01",
        valid_to="2102-01-01",
        principal_id="alice",
        scopes=SCOPES,
    )
    geography.project_boundaries(
        "global",
        scheme="de-bt-wahlkreis",
        boundary_vintage="btw2103",
        feature_collection=VINTAGE_2103,
        id_property="WKR_NR",
        title_property="WKR_NAME",
        valid_from="2102-01-01",
        principal_id="alice",
        scopes=SCOPES,
    )
    yield geography
    conn.close()


def test_each_vintage_is_its_own_collection_with_provider_native_id_and_crs(geo):
    features = GeospatialFeatureStore(geo.conn, initialize=False)
    coverage = features.collection_coverage(
        "global", collection_name("de-bt-wahlkreis", "btw2025"), scopes=GEO
    )
    assert coverage["active"] == 2 and coverage["completeness"] == "complete"
    row = geo.conn.execute(
        "SELECT feature_id, provider, native_id FROM geospatial_features WHERE collection=? ORDER BY native_id",
        [collection_name("de-bt-wahlkreis", "btw2103")],
    ).fetchall()
    assert [(r[1], r[2]) for r in row] == [
        ("bundeswahlleiterin", "001"),
        ("bundeswahlleiterin", "002"),
    ]
    feature = features.feature("global", row[0][0], scopes=GEO)
    assert feature["current"]["source_crs"] == "EPSG:25832"
    assert (
        feature["current"]["provenance"]["source_id"]
        == "elections:de-bt-wahlkreis:btw2103"
    )
    again = geo.project_boundaries(
        "global",
        scheme="de-bt-wahlkreis",
        boundary_vintage="btw2025",
        feature_collection=VINTAGE_2025,
        id_property="WKR_NR",
        valid_from="2023-01-01",
        valid_to="2102-01-01",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert set(again["states"]) <= {"replayed", "unchanged"}  # idempotent projection


def test_a_point_resolves_to_different_constituencies_across_a_boundary_change(geo):
    point = wgs84(507_500)
    before = geo.results_at_point(
        "global",
        scheme="de-bt-wahlkreis",
        point=point,
        as_of="2099-04-01",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert (
        before["status"] == "resolved"
        and before["boundary"]["boundary_vintage"] == "btw2025"
    )
    (item,) = before["constituencies"]
    assert (
        item["constituency"]["native_id"] == "001"
        and item["constituency"]["boundary_vintage"] == "btw2025"
    )
    kinds = {r["contest"]["ballot"]: r["current"]["kind"] for r in item["results"]}
    assert kinds == {"first-vote": "certified", "second-vote": "certified"}
    assert all(r["current"]["source_revision"]["file_sha256"] for r in item["results"])
    assert (
        before["boundary"]["receipt_id"]
        and before["boundary"]["features"][0]["revision_id"]
    )
    after = geo.results_at_point(
        "global",
        scheme="de-bt-wahlkreis",
        point=point,
        as_of="2103-04-01",
        principal_id="alice",
        scopes=SCOPES,
    )
    (item,) = after["constituencies"]
    assert (
        item["constituency"]["native_id"] == "002"
        and item["constituency"]["boundary_vintage"] == "btw2103"
    )
    assert {r["contest"]["election_id"] for r in item["results"]} == {h.DE_NEXT}
    # The result stored for the 2099 contest is untouched by the boundary change.
    replay = GeospatialFeatureStore(geo.conn, initialize=False).replay_within(
        "global", before["boundary"]["receipt_id"], scopes=GEO
    )
    assert replay["deterministic"]
    assert forbidden_keys(before) == [] and forbidden_keys(after) == []


def test_dates_before_any_vintage_and_points_outside_stay_unresolved(geo):
    early = geo.results_at_point(
        "global",
        scheme="de-bt-wahlkreis",
        point=wgs84(507_500),
        as_of="2020-01-01",
        principal_id="a",
        scopes=SCOPES,
    )
    assert early["status"] == "no-boundary-vintage"
    outside = geo.results_at_point(
        "global",
        scheme="de-bt-wahlkreis",
        point=wgs84(530_000),
        as_of="2099-04-01",
        principal_id="a",
        scopes=SCOPES,
    )
    assert (
        outside["status"] == "outside-all-boundaries"
        and outside["constituencies"] == []
    )
    with pytest.raises(ElectionError) as exc:
        geo.results_at_point(
            "global",
            scheme="de-bt-wahlkreis",
            point=wgs84(507_500),
            as_of="2099-04-01",
            principal_id="a",
            scopes=h.SCOPES,
        )
    assert exc.value.code == "unauthorized"


def test_vintage_windows_and_references_are_checked(geo):
    with pytest.raises(ElectionError) as exc:
        geo.project_boundaries(
            "global",
            scheme="de-bt-wahlkreis",
            boundary_vintage="btw2025",
            feature_collection=VINTAGE_2025,
            id_property="WKR_NR",
            valid_from="2023-01-01",
            source_crs="EPSG:4326",
            principal_id="a",
            scopes=SCOPES,
        )
    assert exc.value.code == "crs_mismatch"
    with pytest.raises(ElectionError) as exc:
        geo.project_boundaries(
            "global",
            scheme="de-bt-wahlkreis",
            boundary_vintage="btw2103",
            feature_collection=VINTAGE_2103,
            id_property="WKR_NR",
            valid_from="2090-01-01",
            principal_id="a",
            scopes=SCOPES,
        )
    assert exc.value.code == "overlapping_vintages"
    extra = boundaries(
        [(1, "Musterstadt", 500_000, 505_000), (2, "Beispielkreis", 505_000, 520_000)]
    )
    extra["features"].append({**extra["features"][0], "properties": {"WKR_NR": "n/a"}})
    result = geo.project_boundaries(
        "global",
        scheme="de-bt-wahlkreis",
        boundary_vintage="btw2103",
        feature_collection=extra,
        id_property="WKR_NR",
        valid_from="2102-01-01",
        principal_id="a",
        scopes=SCOPES,
    )
    assert result["unreadable_ids"] == [
        {"index": 2, "id_property": "WKR_NR", "value": "n/a"}
    ]
    with pytest.raises(ElectionError) as exc:
        geo.project_boundaries(
            "global",
            scheme="gb-ons-pcon",
            boundary_vintage="pcon2024",
            feature_collection=extra,
            id_property="WKR_NR",
            valid_from="2024-01-01",
            principal_id="a",
            scopes=SCOPES,
        )
    assert exc.value.code == "no_geometry_reference"


def test_existing_collections_are_registered_and_queried_in_place(geo):
    features = GeospatialFeatureStore(geo.conn, initialize=False)
    features.import_feature_collection(
        "berlin-geo",
        boundaries([(1, "Mitte", 500_000, 510_000)]),
        provider="alkis",
        collection="alkis:bezirke",
        source_crs="EPSG:25832",
        id_property="WKR_NR",
        principal_id="alice",
        scopes=GEO,
        snapshot="complete",
    )
    before = geo.conn.execute("SELECT count(*) FROM geospatial_features").fetchone()[0]
    registered = geo.register_collection(
        "global",
        scheme="de-be-bezirk",
        boundary_vintage="bezirke2001",
        collection="alkis:bezirke",
        provider="alkis",
        feature_namespace="berlin-geo",
        valid_from="2001-01-01",
        principal_id="a",
        scopes=SCOPES,
    )
    assert (
        registered["origin"] == "registered"
        and registered["collection"] == "alkis:bezirke"
    )
    assert (
        geo.conn.execute("SELECT count(*) FROM geospatial_features").fetchone()[0]
        == before
    )  # nothing copied


def test_constituency_place_links_are_reviewable_and_ambiguity_stays_unresolved(geo):
    places = GeospatialStore(geo.conn)
    for key in ("a", "b"):
        places.register_place(
            "geo",
            "Musterstadt",
            "district",
            names=[{"value": "Musterstadt", "language": "de"}],
            source_ids={"fixture": key},
            parent_ids=[],
            principal_id="alice",
            scopes=GEO,
            place_key=key,
        )
    (constituency,) = geo.store.find_constituency(
        "global", "de-bt-wahlkreis", "1", "btw2025"
    )
    linked = geo.link_place(
        "global",
        constituency["constituency_id"],
        geo_namespace="geo",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert (
        linked["state"] == "unresolved"
        and linked["links"][0]["resolution_status"] == "ambiguous"
    )
    chosen = linked["links"][0]["candidates"][0]
    places.review(
        "geo",
        linked["resolution"]["resolution_id"],
        "accept",
        selected_place_id=chosen,
        reason="official gazetteer",
        principal_id="bob",
        scopes=GEO,
    )
    reviewed = geo.place("global", constituency["constituency_id"], scopes=SCOPES)
    assert reviewed["state"] == "linked" and reviewed["place_id"] == chosen
    places.review(
        "geo",
        linked["resolution"]["resolution_id"],
        "reject",
        selected_place_id=None,
        reason="wrong district",
        principal_id="bob",
        scopes=GEO,
    )
    assert (
        geo.place("global", constituency["constituency_id"], scopes=SCOPES)["state"]
        == "unresolved"
    )
