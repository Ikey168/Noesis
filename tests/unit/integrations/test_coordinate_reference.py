import json
import math
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

pytest.importorskip("pyproj")

from src.integrations.spatial import transform_geometry
from src.kb.geospatial import GeospatialStore


def reference():
    return json.loads(
        Path("tests/fixtures/integrations/berlin-coordinate-reference.json").read_text()
    )


def decimal_dms(values):
    return values[0] + values[1] / 60 + values[2] / 3600


def test_published_berlin_coordinate_pairs_axis_order_and_roundtrip():
    fixture = reference()
    for point in fixture["points"]:
        original = {
            "type": "Point",
            "coordinates": [
                decimal_dms(point["longitude_dms"]),
                decimal_dms(point["latitude_dms"]),
            ],
        }
        transformed = transform_geometry(original, "EPSG:4326", "EPSG:25833")
        result = transformed["result"]
        assert (
            math.dist(result["geometry"]["coordinates"], point["epsg25833"])
            < fixture["tolerance_m"]
        )
        assert result["axis_order"] == "x,y"
        assert (
            result["pipeline"]
            and result["proj_database"]
            and result["proj_database_date"]
        )
        assert result["grids"] == [] and result["network_enabled"] is False
        back = transform_geometry(result["geometry"], "EPSG:25833")
        assert back["result"]["geometry"]["coordinates"] == pytest.approx(
            original["coordinates"], abs=1e-9
        )
        assert (
            transform_geometry(original, "EPSG:4326", "EPSG:25833")["sha256"]
            == transformed["sha256"]
        )


def test_projected_import_retains_original_receipt_and_replays():
    fixture = reference()
    source = {"type": "Point", "coordinates": fixture["points"][0]["epsg25833"]}
    conn = duckdb.connect()
    store = GeospatialStore(conn, now=lambda: 100)
    options = {
        "source_crs": "EPSG:25833",
        "place_id": None,
        "precision_m": 3,
        "simplified_from": None,
        "disputed": False,
        "admin_hierarchy": [],
        "source": {"url": fixture["source_url"], "sha256": fixture["source_sha256"]},
        "evidence": [{"citation": fixture["source_url"], "page": 25}],
        "principal_id": "mapper",
        "scopes": {"knowledge:geospatial:write"},
    }
    imported = store.import_projected_geometry("berlin", source, **options)
    assert imported["source"]["coordinate_transform"]["request"]["geometry"] == source
    repeated = store.import_projected_geometry("berlin", source, **options)
    assert repeated["idempotent"] and repeated["geometry_id"] == imported["geometry_id"]
    conn.close()


def test_unavailable_grid_and_invalid_coordinates_fail_explicitly(monkeypatch):
    import pyproj.transformer

    with pytest.raises(ValueError, match="longitude/latitude"):
        transform_geometry(
            {"type": "Point", "coordinates": [13, 95]}, "EPSG:4326", "EPSG:25833"
        )
    monkeypatch.setattr(
        pyproj.transformer,
        "TransformerGroup",
        lambda *a, **k: SimpleNamespace(best_available=False, transformers=[object()]),
    )
    with pytest.raises(ValueError, match="grid is unavailable"):
        transform_geometry(
            {"type": "Point", "coordinates": [13, 52]}, "EPSG:4326", "EPSG:25833"
        )
