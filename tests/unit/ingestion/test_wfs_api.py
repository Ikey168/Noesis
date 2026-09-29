"""Native WFS 2.0.0 adapter against captured Berlin envelopes (#1696, #1698)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import SourcePackError
from src.ingestion.wfs_api import (
    WfsFeatureAdapter,
    _uncursor,
    fixture_transport,
    replay_native_fixture,
    service_exception,
    validate_wfs_declaration,
)
from tests.unit.geospatial_pack_helpers import (
    FIXTURES,
    ROOT,
    WfsServer,
    manifest,
    point,
    source,
)


def _fixture(name):
    return json.loads((ROOT / f"tests/fixtures/source_packs/geospatial-{name}.json").read_text())


def _walk(adapter, *, parameters=None, limit=5000):
    pages, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": "features", "parameters": parameters or {}, "limit": limit},
            cursor=cursor,
        )
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


def test_manifest_pins_version_format_crs_axis_order_sort_and_terms():
    value = manifest()
    wfs_sources = [item for item in value["sources"] if item["connector"] == "wfs"]
    assert len(wfs_sources) == 2
    for item in wfs_sources:
        wfs = validate_wfs_declaration(item)
        assert wfs["version"] == "2.0.0"
        assert wfs["output_format"] == "application/json"
        assert wfs["srs_name"] == "urn:ogc:def:crs:EPSG::25833"
        assert wfs["axis_order"] == "east_north" and wfs["sort_by"]
        assert item["license"]["id"] == "dl-de-zero-2.0"
        assert item["geospatial"]["precision"]["status"] == "unknown"
        assert item["geospatial"]["metadata_url"].startswith("https://gdi.berlin.de/geonetwork/")
    bad = dict(value["sources"][0])
    bad["wfs"] = {**bad["wfs"], "output_format": "application/gml+xml; version=3.2"}
    with pytest.raises(SourcePackError):
        validate_wfs_declaration(bad)


def test_captured_bezirke_pages_stop_on_number_matched_not_the_providers_extra_next_link():
    selected = source(manifest(), "berlin-bezirksgrenzen")
    fixture = _fixture("bezirksgrenzen")
    adapter = WfsFeatureAdapter(selected, transport=fixture_transport(fixture["native_pages"]))
    pages = _walk(adapter)
    assert len(pages) == 2
    assert pages[-1].receipt["declared_next"] is True  # provider quirk captured live
    assert pages[-1].receipt["final_page"] is True
    ids = [record["feature"]["native_id"] for page in pages for record in page.records]
    assert ids == [f"bezirksgrenzen.110000{index:02d}" for index in range(1, 13)]
    titles = {record["title"] for page in pages for record in page.records}
    assert {"Mitte", "Friedrichshain-Kreuzberg", "Spandau"} <= titles
    assert pages[0].receipt["response_sha256"] == fixture["native_pages"][0]["body_sha256"]


def test_captured_school_pages_resume_from_a_checkpoint_cursor():
    selected = source(manifest(), "berlin-schulen")
    fixture = _fixture("schulen")
    adapter = WfsFeatureAdapter(selected, transport=fixture_transport(fixture["native_pages"]))
    first = adapter.fetch_page({"operation": "features", "limit": 5000}, cursor=None)
    assert _uncursor(first.next_cursor)["start"] == 250
    resumed = WfsFeatureAdapter(selected, transport=fixture_transport(fixture["native_pages"]))
    rest = []
    cursor = first.next_cursor
    while cursor:
        page = resumed.fetch_page({"operation": "features", "limit": 5000}, cursor=cursor)
        rest.append(page)
        cursor = page.next_cursor
    assert [page.receipt["start_index"] for page in rest] == [250, 500, 750]
    assert sum(len(page.records) for page in [first, *rest]) == 930


def test_replay_native_fixture_decodes_every_captured_feature():
    value = manifest()
    assert len(replay_native_fixture(source(value, "berlin-schulen"), _fixture("schulen"))) == 930
    assert len(replay_native_fixture(source(value, "berlin-bezirksgrenzen"), _fixture("bezirksgrenzen"))) == 12


def test_requests_are_pinned_and_bbox_is_explicit_in_source_crs():
    server = WfsServer([point("a", 0, 0), point("b", 5000, 5000)])
    adapter = WfsFeatureAdapter(source(manifest(), "berlin-schulen"), transport=server)
    page = adapter.fetch_page(
        {"operation": "features", "parameters": {"bbox": [389_000, 5_819_000, 391_000, 5_821_000]},
         "limit": 10},
        cursor=None,
    )
    call = server.calls[0]
    assert {k: call[k] for k in ("SERVICE", "VERSION", "REQUEST", "OUTPUTFORMAT", "SRSNAME", "SORTBY")} == {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "OUTPUTFORMAT": "application/json", "SRSNAME": "urn:ogc:def:crs:EPSG::25833",
        "SORTBY": "bsn",
    }
    assert call["BBOX"].endswith(",urn:ogc:def:crs:EPSG::25833")
    assert call["COUNT"] == 10
    assert [r["feature"]["native_id"] for r in page.records] == ["a"]
    assert page.receipt["complete_scope"] is False
    for bad in ({"bbox": [1, 2, 3]}, {"bbox": [3, 3, 1, 1]}, {"cql_filter": "x"}):
        with pytest.raises(SourcePackError) as caught:
            adapter.fetch_page({"operation": "features", "parameters": bad}, cursor=None)
        assert caught.value.code == "parameter_forbidden"


def test_page_size_caps_count_and_cursor_rejects_a_different_scope():
    server = WfsServer([point(f"p{i}", i, i) for i in range(5)])
    adapter = WfsFeatureAdapter(source(manifest(page_size=2), "berlin-schulen"), transport=server)
    pages = _walk(adapter)
    assert [call["COUNT"] for call in server.calls] == [2, 2, 2]
    assert [call["STARTINDEX"] for call in server.calls] == [0, 2, 4]
    first = adapter.fetch_page({"operation": "features", "limit": 10}, cursor=None)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "features", "parameters": {"bbox": [0, 0, 1, 1]}, "limit": 10},
            cursor=first.next_cursor,
        )
    assert caught.value.code == "cursor_drift"
    assert len(pages) == 3


def test_real_exception_report_is_provider_drift_and_processing_failures_are_retryable():
    raw = (FIXTURES / "wfs-exception-invalid-sortby.xml").read_bytes()
    error = service_exception(raw)
    assert error.code == "schema_drift" and "InvalidParameterValue" in str(error)
    transient = service_exception(
        b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
        b'<ows:Exception exceptionCode="OperationProcessingFailed"/></ows:ExceptionReport>'
    )
    assert transient.code == "source_unavailable"
    adapter = WfsFeatureAdapter(
        source(manifest(), "berlin-schulen"),
        transport=lambda **_: {"status": 400, "headers": {}, "content": raw},
    )
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "features"}, cursor=None)
    assert caught.value.code == "schema_drift"


def test_rate_limits_repeated_pages_shrinking_collections_and_count_mismatch():
    selected = source(manifest(page_size=2), "berlin-schulen")
    limited = WfsFeatureAdapter(
        selected, transport=lambda **_: {"status": 429, "headers": {"Retry-After": "3"}, "content": b""}
    )
    with pytest.raises(SourcePackError) as caught:
        limited.fetch_page({"operation": "features"}, cursor=None)
    assert caught.value.code == "rate_limited" and caught.value.details["retry_after_ms"] == 3000

    features = [point(f"p{i}", i, i) for i in range(4)]
    server = WfsServer(features)
    adapter = WfsFeatureAdapter(selected, transport=server)
    first = adapter.fetch_page({"operation": "features", "limit": 2}, cursor=None)
    repeating = WfsFeatureAdapter(
        selected,
        transport=lambda **kw: server(**{**kw, "params": {**kw["params"], "STARTINDEX": 0}}),
    )
    with pytest.raises(SourcePackError) as caught:
        repeating.fetch_page({"operation": "features", "limit": 2}, cursor=first.next_cursor)
    assert "repeated" in str(caught.value)

    server.features = features[:3]
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "features", "limit": 2}, cursor=first.next_cursor)
    assert "changed size" in str(caught.value)

    def lying(**kwargs):
        response = WfsServer(features)(**kwargs)
        body = json.loads(response["content"])
        body["numberReturned"] = 9
        return {**response, "content": json.dumps(body).encode()}

    with pytest.raises(SourcePackError) as caught:
        WfsFeatureAdapter(selected, transport=lying).fetch_page({"operation": "features"}, cursor=None)
    assert caught.value.code == "schema_drift"


def test_undeclared_operations_and_controls_are_refused():
    adapter = WfsFeatureAdapter(source(manifest(), "berlin-schulen"), transport=WfsServer([]))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "search"}, cursor=None)
    assert caught.value.code == "operation_forbidden"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "features", "url": "https://evil"}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "features"}, cursor="%%%")
    assert caught.value.code == "cursor_drift"
