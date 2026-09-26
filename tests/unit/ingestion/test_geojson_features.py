"""Bounded GeoJSON FeatureCollection decoding and local import (#1697)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.geojson_features import (
    FeatureBudget,
    GeoJsonFeatureAdapter,
    decode_feature_collection,
    feature_key,
    load_local_feature_collection,
)
from src.ingestion.source_packs import SourcePackError, validate_source_pack
from tests.unit.geospatial_pack_helpers import FIXTURES, point, raw_manifest, square

DECODE = {"provider": "Test", "collection": "facilities", "source_crs": "EPSG:25833"}


def collection(*features, **extra):
    return {"type": "FeatureCollection", "features": list(features), **extra}


def test_decodes_identity_properties_geometry_and_source_hash():
    raw = json.dumps(collection(point("a", 1, 2, kind="school"), square("b", 0, 0, 10))).encode()
    decoded = decode_feature_collection(raw, title_property="name", **DECODE)
    assert decoded["rejections"] == []
    first = decoded["records"][0]
    assert first["id"] == feature_key("Test", "facilities", "a")
    assert first["title"] == "a"
    feature = first["feature"]
    assert feature["native_id"] == "a" and feature["properties"]["kind"] == "school"
    assert feature["source_geometry"] == feature["geometry"]
    assert feature["source_sha256"] == decoded["source_sha256"]
    assert decoded["source_bytes"] == len(raw) and decoded["positions"] == 6


def test_north_east_axis_order_is_swapped_but_the_source_geometry_is_retained():
    feature = {"type": "Feature", "id": "p", "properties": {},
               "geometry": {"type": "Point", "coordinates": [52.52, 13.40]}}
    decoded = decode_feature_collection(
        collection(feature), provider="T", collection="c", source_crs="EPSG:4326",
        axis_order="north_east",
    )
    record = decoded["records"][0]["feature"]
    assert record["geometry"]["coordinates"] == [13.40, 52.52]
    assert record["source_geometry"]["coordinates"] == [52.52, 13.40]
    assert record["axis_order"] == "north_east"


def test_missing_duplicate_null_unsupported_and_invalid_features_are_rejected_explicitly():
    unclosed = square("open", 0, 0, 10)
    unclosed["geometry"]["coordinates"][0].pop()
    decoded = decode_feature_collection(
        collection(
            point("dup", 0, 0),
            point("dup", 1, 1),
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [1, 1]}},
            {"type": "Feature", "id": "null", "properties": {}, "geometry": None},
            {"type": "Feature", "id": "multi", "properties": {},
             "geometry": {"type": "MultiPoint", "coordinates": [[1, 1]]}},
            {"type": "Feature", "id": "bag", "properties": {},
             "geometry": {"type": "GeometryCollection", "geometries": []}},
            unclosed,
            {"type": "Feature", "id": "nan", "properties": {},
             "geometry": {"type": "Point", "coordinates": [1, "x"]}},
            "not a feature",
        ),
        **DECODE,
    )
    assert [item["id"] for item in decoded["records"]] == [feature_key("Test", "facilities", "dup")]
    codes = [item["rejection"]["code"] for item in decoded["rejections"]]
    assert codes == ["duplicate_id", "missing_id", "null_geometry", "unsupported_geometry",
                     "unsupported_geometry", "invalid_geometry", "invalid_geometry",
                     "invalid_feature"]
    assert decoded["rejections"][0]["native_feature"]["geometry"]["coordinates"]


def test_malformed_envelopes_fail_as_provider_drift():
    for raw in (b"not json", b'{"type":"Feature"}', b'{"type":"FeatureCollection"}'):
        with pytest.raises(SourcePackError) as caught:
            decode_feature_collection(raw, **DECODE)
        assert caught.value.code == "schema_drift"


@pytest.mark.parametrize(
    ("budget", "code"),
    [
        (FeatureBudget(max_features=1), "budget_exhausted"),
        (FeatureBudget(max_bytes=50), "response_too_large"),
        (FeatureBudget(max_coordinates=2), "budget_exhausted"),
        (FeatureBudget(max_depth=3), "budget_exhausted"),
    ],
)
def test_feature_byte_coordinate_and_depth_budgets(budget, code):
    raw = json.dumps(collection(point("a", 0, 0), point("b", 1, 1), point("c", 2, 2)))
    with pytest.raises(SourcePackError) as caught:
        decode_feature_collection(raw, budget=budget, **DECODE)
    assert caught.value.code == code


def test_budget_bounds_are_validated():
    with pytest.raises(SourcePackError):
        FeatureBudget(max_features=0)


def test_local_import_is_confined_to_the_configured_root(tmp_path, monkeypatch):
    inside = tmp_path / "imports"
    inside.mkdir()
    (inside / "ok.geojson").write_bytes(b'{"type":"FeatureCollection","features":[]}')
    (tmp_path / "secret.geojson").write_text("{}")
    monkeypatch.delenv("NOESIS_GEOSPATIAL_IMPORT_ROOT", raising=False)
    with pytest.raises(SourcePackError) as caught:
        load_local_feature_collection("ok.geojson")
    assert caught.value.code == "import_root_unconfigured"
    monkeypatch.setenv("NOESIS_GEOSPATIAL_IMPORT_ROOT", str(inside))
    assert load_local_feature_collection("ok.geojson").startswith(b"{")
    for path, code in (("../secret.geojson", "unsafe_import_path"),
                       (str(tmp_path / "secret.geojson"), "unsafe_import_path"),
                       ("missing.geojson", "not_found")):
        with pytest.raises(SourcePackError) as caught:
            load_local_feature_collection(path)
        assert caught.value.code == code
    with pytest.raises(SourcePackError) as caught:
        load_local_feature_collection("ok.geojson", max_bytes=5)
    assert caught.value.code == "response_too_large"


def test_real_ortsteile_fixture_decodes_three_multipolygons():
    raw = (FIXTURES / "ortsteile-mitte-subset.geojson").read_bytes()
    decoded = decode_feature_collection(
        raw, provider="Geoportal Berlin", collection="alkis_ortsteile:ortsteile",
        source_crs="urn:ogc:def:crs:EPSG::25833", title_property="nam",
    )
    assert sorted(item["title"] for item in decoded["records"]) == ["Hansaviertel", "Mitte", "Moabit"]
    assert {item["feature"]["geometry"]["type"] for item in decoded["records"]} == {"MultiPolygon"}


def _geojson_source():
    value = raw_manifest()
    source = dict(value["sources"][0])
    source.pop("wfs")
    source.update({
        "source_id": "static-geojson", "connector": "geojson",
        "endpoint": "https://example.org/features.geojson",
        "geospatial": {"collection": "static", "source_crs": "EPSG:25833",
                       "axis_order": "east_north", "snapshot": "complete"},
    })
    value["sources"] = [source]
    return validate_source_pack(value)["sources"][0]


def test_https_geojson_adapter_is_single_page_and_classifies_failures():
    source = _geojson_source()
    body = json.dumps(collection(point("a", 0, 0))).encode()
    adapter = GeoJsonFeatureAdapter(
        source, transport=lambda **_: {"status": 200, "headers": {}, "content": body}
    )
    page = adapter.fetch_page({"operation": "features"}, cursor=None)
    assert page.next_cursor is None and len(page.records) == 1
    assert page.receipt["complete_scope"] is True
    with pytest.raises(SourcePackError):
        adapter.fetch_page({"operation": "features", "parameters": {"bbox": [0, 0, 1, 1]}}, cursor=None)
    for status, code in ((429, "rate_limited"), (403, "authentication_failed"), (503, "source_unavailable")):
        failing = GeoJsonFeatureAdapter(
            source, transport=lambda status=status, **_: {"status": status, "headers": {"Retry-After": "2"}, "content": b""}
        )
        with pytest.raises(SourcePackError) as caught:
            failing.fetch_page({"operation": "features"}, cursor=None)
        assert caught.value.code == code
