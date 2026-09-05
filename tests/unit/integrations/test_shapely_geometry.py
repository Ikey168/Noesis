import gzip
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from src.integrations.spatial import simplify_geometry, topology, transform_geometry
from src.kb.geospatial import CALCULATE_SCOPE, READ_SCOPE, WRITE_SCOPE, GeospatialStore

OUTER = [[13, 52], [13.1, 52], [13.1, 52.1], [13, 52.1], [13, 52]]
HOLE = [[13.02, 52.02], [13.04, 52.02], [13.04, 52.04], [13.02, 52.04], [13.02, 52.02]]
POLYGON = {"type": "Polygon", "coordinates": [OUTER, HOLE]}


def store_geometry(store, geometry):
    return store.store_geometry(
        "berlin",
        geometry,
        place_id=None,
        crs="EPSG:4326",
        precision_m=1,
        simplified_from=None,
        disputed=False,
        admin_hierarchy=["DE-BE"],
        source={"revision": "authored:1"},
        evidence=[],
        principal_id="fixture",
        scopes={WRITE_SCOPE},
        observed_at_ms=100,
    )


def test_analytic_reference_holes_boundaries_and_contained_intersection():
    store = GeospatialStore(duckdb.connect())
    polygon = store_geometry(store, POLYGON)
    for point, contains, covers in [
        ([13.01, 52.01], True, True),
        ([13.03, 52.03], False, False),
        ([13, 52.05], False, True),
        ([13.02, 52.03], False, True),
    ]:
        for operation, expected in (("contains", contains), ("covers", covers)):
            result = store.relation(
                "berlin",
                operation,
                polygon["geometry_id"],
                point,
                backend="shapely",
                scopes={CALCULATE_SCOPE},
                principal_id="fixture",
            )
            assert result["result"][operation] is expected
    # Independent rectangle geometry: no boundary crossing, but nonempty interior overlap.
    inner = store_geometry(
        store,
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [13.06, 52.06],
                    [13.07, 52.06],
                    [13.07, 52.07],
                    [13.06, 52.07],
                    [13.06, 52.06],
                ]
            ],
        },
    )
    native = store.relation(
        "berlin",
        "intersects",
        polygon["geometry_id"],
        inner["geometry_id"],
        scopes={CALCULATE_SCOPE},
        principal_id="fixture",
    )
    robust = store.relation(
        "berlin",
        "intersects",
        polygon["geometry_id"],
        inner["geometry_id"],
        backend="shapely",
        scopes={CALCULATE_SCOPE},
        principal_id="fixture",
    )
    assert native["result"]["intersects"] is False
    assert robust["result"]["intersects"] is True
    store.conn.close()


def test_multipart_contract_and_native_berlin_projected_simplification():
    from shapely.geometry import shape

    fixture = json.loads(
        gzip.decompress(
            Path("tests/fixtures/integrations/berlin-mitte-native.json.gz").read_bytes()
        )
    )
    native = fixture["features"][0]["geometry"]
    geographic = transform_geometry(native, "EPSG:25833")["result"]["geometry"]
    store = GeospatialStore(duckdb.connect())
    original = store.import_projected_geometry(
        "berlin",
        native,
        source_crs="EPSG:25833",
        place_id=None,
        precision_m=1,
        simplified_from=None,
        disputed=False,
        admin_hierarchy=["DE-BE"],
        source={"revision": "native-mitte:2026-09-05"},
        evidence=[],
        principal_id="fixture",
        scopes={WRITE_SCOPE},
        observed_at_ms=100,
    )
    assert original["source"]["coordinate_transform"]["request"]["geometry"] == native
    schema = json.loads(
        Path(
            "contracts/schemas/jsonschema/noesis-geospatial-geometry-v2.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(original)
    simplified = store.simplify(
        "berlin",
        original["geometry_id"],
        10,
        backend="shapely",
        projected_crs="EPSG:25833",
        scopes={WRITE_SCOPE},
        principal_id="fixture",
    )
    run = simplified["source"]["simplification"]
    assert run["result"]["discrete_hausdorff_m"] <= 10
    assert run["result"]["effective_tolerance_m"] <= 10
    assert simplified["precision_m"] == 13
    assert simplified["simplified_from"] == original["geometry_id"]
    assert shape(simplified["geometry"]).is_valid
    assert (
        store.geometry("berlin", original["geometry_id"], scopes={READ_SCOPE})[
            "geometry"
        ]
        == geographic
    )
    assert (
        store.simplify(
            "berlin",
            original["geometry_id"],
            10,
            backend="shapely",
            projected_crs="EPSG:25833",
            scopes={WRITE_SCOPE},
            principal_id="fixture",
        )["geometry_id"]
        == simplified["geometry_id"]
    )
    # Separate authored components exercise a truly disconnected MultiPolygon.
    multipart = store_geometry(
        store,
        {
            "type": "MultiPolygon",
            "coordinates": [
                [OUTER],
                [[[13.2, 52], [13.3, 52], [13.3, 52.1], [13.2, 52.1], [13.2, 52]]],
            ],
        },
    )
    assert store.relation(
        "berlin",
        "contains",
        multipart["geometry_id"],
        [13.25, 52.05],
        backend="shapely",
        scopes={CALCULATE_SCOPE},
        principal_id="fixture",
    )["result"]["contains"]
    with pytest.raises(ValueError, match="Shapely"):
        store.simplify(
            "berlin",
            multipart["geometry_id"],
            10,
            scopes={WRITE_SCOPE},
            principal_id="fixture",
        )
    store.conn.close()


def test_invalid_rings_collection_dateline_and_projection_fail_explicitly():
    point = {"type": "Point", "coordinates": [13, 52]}
    for invalid in [
        {
            "type": "Polygon",
            "coordinates": [[[13, 52], [13.1, 52.1], [13, 52.1], [13.1, 52], [13, 52]]],
        },
        {"type": "Polygon", "coordinates": [OUTER[:-1]]},
        {"type": "GeometryCollection", "geometries": [point]},
        {"type": "LineString", "coordinates": [[179, 52], [-179, 52]]},
    ]:
        with pytest.raises(ValueError):
            topology("intersects", invalid, point)
    for crs in (None, "EPSG:4326", "EPSG:3857", "EPSG:25832"):
        with pytest.raises(ValueError):
            simplify_geometry(POLYGON, 10, projected_crs=crs)
    for tolerance in (-1, float("nan"), 10001):
        with pytest.raises(ValueError):
            simplify_geometry(POLYGON, tolerance, projected_crs="EPSG:25833")
    western_line = {
        "type": "LineString",
        "coordinates": [[7, 51], [7.0001, 51.0001], [7.0002, 51.0002]],
    }
    reduced = simplify_geometry(western_line, 10, projected_crs="EPSG:25832")
    assert len(reduced["result"]["geometry"]["coordinates"]) == 2
