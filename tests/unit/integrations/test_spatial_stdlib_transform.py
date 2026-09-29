"""Offline UTM transform fallback against a server-side reference (#1699)."""

from __future__ import annotations

import json
import math

import pytest

from src.integrations.common import IntegrationError
from src.integrations.spatial import _stdlib_transform, transform_capability
from tests.unit.geospatial_pack_helpers import FIXTURES, ROOT


def _metres(a, b):
    lat = math.radians(b[1])
    return math.hypot((a[0] - b[0]) * 111_320 * math.cos(lat), (a[1] - b[1]) * 110_574)


def test_etrs89_utm33_inverse_matches_the_provider_transform_within_a_centimetre():
    reference = json.loads((FIXTURES / "schulen-epsg4258-reference.json").read_text())
    native = {}
    for page in json.loads(
        (ROOT / "tests/fixtures/source_packs/geospatial-schulen.json").read_text()
    )["native_pages"]:
        for feature in json.loads(page["body"])["features"]:
            native[feature["id"]] = feature["geometry"]
    worst = 0.0
    for item in reference["points"]:
        result = _stdlib_transform(native[item["id"]], "urn:ogc:def:crs:EPSG::25833", "EPSG:4326")
        worst = max(worst, _metres(result["result"]["geometry"]["coordinates"], item["coordinates"]))
    assert len(reference["points"]) == 93
    assert worst < 0.01


def test_receipt_declares_backend_pipeline_and_datum_accuracy():
    geometry = {"type": "Point", "coordinates": [392_000.0, 5_820_000.0]}
    first = _stdlib_transform(geometry, "EPSG:25833", "EPSG:4326")
    again = _stdlib_transform(geometry, "EPSG:25833", "EPSG:4326")
    assert first["sha256"] == again["sha256"]
    assert first["producer"] == {"backend": "noesis-stdlib-utm", "version": "1.0.0"}
    result = first["result"]
    assert result["source_crs"] == "EPSG:25833" and result["axis_order"] == "x,y"
    assert result["accuracy_m"] == 1.0
    assert "EPSG:1149" in result["datum_transformation"]
    wgs = _stdlib_transform(geometry, "EPSG:32633", "EPSG:4326")["result"]
    assert wgs["accuracy_m"] == 0.0 and wgs["datum_transformation"] is None


def test_polygon_holes_and_multipolygons_keep_their_structure():
    ring = [[390_000.0, 5_820_000.0], [391_000.0, 5_820_000.0], [391_000.0, 5_821_000.0],
            [390_000.0, 5_821_000.0], [390_000.0, 5_820_000.0]]
    hole = [[390_400.0, 5_820_400.0], [390_400.0, 5_820_600.0], [390_600.0, 5_820_600.0],
            [390_600.0, 5_820_400.0], [390_400.0, 5_820_400.0]]
    result = _stdlib_transform(
        {"type": "MultiPolygon", "coordinates": [[ring, hole], [ring]]}, "EPSG:25833", "EPSG:4326"
    )["result"]["geometry"]
    assert result["type"] == "MultiPolygon"
    assert [len(polygon) for polygon in result["coordinates"]] == [2, 1]
    assert result["coordinates"][0][0][0] == result["coordinates"][0][0][-1]


def test_unsupported_crs_or_grid_needs_pyproj_and_says_so():
    geometry = {"type": "Point", "coordinates": [10.0, 50.0]}
    with pytest.raises(IntegrationError) as caught:
        _stdlib_transform(geometry, "EPSG:31468", "EPSG:4326")  # DHDN needs a grid shift
    assert caught.value.code == "transform_unavailable"
    with pytest.raises(IntegrationError) as caught:
        _stdlib_transform(geometry, "EPSG:4326", "EPSG:25833")
    assert caught.value.code == "transform_unavailable"
    with pytest.raises(IntegrationError) as caught:
        _stdlib_transform({"type": "Point", "coordinates": [-5.0, 1.0]}, "EPSG:25833", "EPSG:4326")
    assert caught.value.code == "transform_failed"


def test_capability_reports_the_available_backend():
    capability = transform_capability()
    assert capability["backend"] in {"pyproj", "noesis-stdlib-utm"}
    if not capability["pyproj_available"]:
        assert "EPSG:25828-25838" in capability["supported_sources"]
        assert capability["action"]


def test_pyproj_agrees_with_the_fallback_when_installed():
    pyproj = pytest.importorskip("pyproj")
    del pyproj
    from src.integrations.spatial import transform_geometry

    geometry = {"type": "Point", "coordinates": [388_591.39999995, 5_820_578.29999966]}
    fallback = _stdlib_transform(geometry, "EPSG:25833", "EPSG:4326")["result"]["geometry"]
    reference = transform_geometry(geometry, "EPSG:25833")["result"]["geometry"]
    assert _metres(fallback["coordinates"], reference["coordinates"]) < 1.0
