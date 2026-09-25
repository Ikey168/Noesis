"""Shared builders for geospatial-pack tests (synthetic WFS pages in EPSG:25833)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.source_pack_runtime import SourcePackRuntime

ROOT = Path(__file__).resolve().parents[2]
PACK_PATH = ROOT / "config/source_packs/geospatial.json"
FIXTURES = ROOT / "tests/fixtures/geospatial/berlin"
SCOPES = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
ORIGIN = (390_000.0, 5_820_000.0)  # central Berlin, ETRS89 / UTM 33N


def raw_manifest() -> dict[str, Any]:
    return json.loads(PACK_PATH.read_text())


def manifest(**wfs_updates: Any) -> dict[str, Any]:
    value = copy.deepcopy(raw_manifest())
    for source in value["sources"]:
        source["wfs"].update(wfs_updates)
    return validate_source_pack(value)


def install(conn, value: dict[str, Any] | None = None, *, clock_start: int = 1_000):
    value = value or manifest()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(clock_start, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for source in value["sources"]:
        runtime.accept_license(value["pack_id"], source["source_id"], principal_id="operator")
    return value, runtime


def source(value: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(item for item in value["sources"] if item["source_id"] == source_id)


def point(native_id: str, dx: float, dy: float, **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": native_id,
        "geometry": {"type": "Point", "coordinates": [ORIGIN[0] + dx, ORIGIN[1] + dy]},
        "properties": {"name": native_id, **properties},
    }


def square(native_id: str, dx: float, dy: float, size: float, *, hole: float | None = None,
           multi: bool = False, **properties: Any) -> dict[str, Any]:
    x, y = ORIGIN[0] + dx, ORIGIN[1] + dy
    ring = [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
    rings = [ring]
    if hole:
        cx, cy = x + size / 2, y + size / 2
        rings.append([[cx - hole, cy - hole], [cx - hole, cy + hole], [cx + hole, cy + hole],
                      [cx + hole, cy - hole], [cx - hole, cy - hole]])
    geometry = (
        {"type": "MultiPolygon", "coordinates": [rings]}
        if multi else {"type": "Polygon", "coordinates": rings}
    )
    return {"type": "Feature", "id": native_id, "geometry": geometry,
            "properties": {"name": native_id, **properties}}


class WfsServer:
    """Serve synthetic GetFeature pages honoring COUNT, STARTINDEX and BBOX."""

    def __init__(self, features: list[dict[str, Any]], *, timestamp: str = "2026-09-25T00:00:00Z"):
        self.features = features
        self.timestamp = timestamp
        self.calls: list[dict[str, Any]] = []
        self.failures: dict[int, dict[str, Any]] = {}

    def __call__(self, *, url, params, headers, timeout):
        del url, headers, timeout
        self.calls.append(dict(params))
        start = int(params["STARTINDEX"])
        if start in self.failures:
            return self.failures.pop(start)
        selected = self.features
        if "BBOX" in params:
            minx, miny, maxx, maxy = (float(v) for v in params["BBOX"].split(",")[:4])
            selected = [
                item for item in selected
                if item["geometry"]["type"] == "Point"
                and minx <= item["geometry"]["coordinates"][0] <= maxx
                and miny <= item["geometry"]["coordinates"][1] <= maxy
            ]
        page = selected[start:start + int(params["COUNT"])]
        body = {
            "type": "FeatureCollection",
            "features": page,
            "totalFeatures": len(selected),
            "numberMatched": len(selected),
            "numberReturned": len(page),
            "timeStamp": self.timestamp,
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::25833"}},
        }
        return {"status": 200, "headers": {}, "content": json.dumps(body).encode()}


def run(runtime, value, key: str, server: WfsServer, *, source_id: str = "berlin-schulen",
        parameters: dict[str, Any] | None = None, **controls: Any) -> dict[str, Any]:
    selected = source(value, source_id)
    adapter = runtime.factory.compile(selected, transport=server)
    request = {
        "pack_id": value["pack_id"],
        "run_key": key,
        "operation": "features",
        "source_ids": [source_id],
        "required_sources": [source_id],
        "parameters": parameters or {},
        "max_results": 5000,
        "max_bytes": 5_000_000,
        "timeout_ms": 120_000,
        **controls,
    }
    return runtime.run(request, principal_id="operator", adapters={source_id: adapter},
                       dns_resolver=PUBLIC_DNS)
