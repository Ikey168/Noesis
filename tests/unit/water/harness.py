"""Offline harness for the Climate and Environment water feature (#2582): authored payloads, real runtime.

Payloads under ``tests/fixtures/source_packs/water-*.json`` (built by
:mod:`tests.unit.water.fixture_builder`) are authored in each provider's
documented response shape; stations, codes and values are fictional. They run
through :class:`WaterSourceAdapter` (the connector the runtime compiles) and
:class:`WaterProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.water_sources import FIXTURE_SECRET, fixture_transport
from src.kb.water_store import WaterProjector, WaterStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-water.json"
NS = "environment"
READ = {"knowledge:environment:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:environment:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:environment:review"}
ALL = REVIEW | {
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:entity-history:read",
    "knowledge:entity-history:write", "knowledge:entity-history:review", "knowledge:entity-history:execute",
    "knowledge:hazards:read", "knowledge:hazards:write", "knowledge:infrastructure:read",
    "knowledge:infrastructure:write", "knowledge:weather:read",
}
SOURCES = ["pegelonline-stations-levels", "usgs-water-data", "eea-wise-wfd-status"]
LATER = json.loads((ROOT / "tests/fixtures/water/later_payloads.json").read_text())
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
# Fictional places: a district around the EXAMPLA gauge and the WB1 geometry, a county around the USGS points.
EXAMPLA_DISTRICT = {"type": "Polygon", "coordinates": [[[13.30, 52.45], [13.50, 52.45], [13.50, 52.60],
                                                        [13.30, 52.60], [13.30, 52.45]]]}
SAMPLE_COUNTY = {"type": "Polygon", "coordinates": [[[-77.30, 38.90], [-76.80, 38.90], [-76.80, 39.20],
                                                     [-77.30, 39.20], [-77.30, 38.90]]]}
EMPTY_MOOR = {"type": "Polygon", "coordinates": [[[10.00, 50.00], [10.10, 50.00], [10.10, 50.10],
                                                  [10.00, 50.10], [10.00, 50.00]]]}
FORBIDDEN = {"forecast", "predicted", "interpolated", "gap_filled", "filled_value", "flood_risk", "risk_score",
             "derived_status", "noesis_status", "merged_status", "overall_status", "trend", "email", "phone",
             "contact", "person", "observer"}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN:
                found.add(key)
            found |= forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= forbidden_keys(item)
    return found


class Env:
    """One in-memory deployment with the water source pack installed and its licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = 1_790_000_000_000  # 2026-09-21
        self.runtime = SourcePackRuntime(self.conn, now=self.tick, sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = WaterStore(self.conn, now=self.tick)
        # Retrieval times follow the deployment clock, so as-of answers are deterministic.
        self.runtime.projectors["noesis-water-record-v1"] = WaterProjector(self.conn, now=self.tick)

    def tick(self) -> int:
        self.clock += 1_000
        return self.clock

    def advance(self, days: float) -> None:
        self.clock += int(days * 86_400_000)

    def adapters(self, later: bool = False) -> dict:
        if not later:
            return self.runtime.fixture_adapters(self.value["pack_id"], ROOT)
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        result = {}
        for source in installed["sources"]:
            overrides = LATER[source["water"]["provider"]]
            pages = copy.deepcopy(json.loads((ROOT / source["fixture"]["path"]).read_text())["native_pages"])
            for page in pages:
                if page["request"] in overrides:
                    page.update(copy.deepcopy(overrides[page["request"]]))
            result[source["source_id"]] = self.runtime.factory.compile(
                source, transport=fixture_transport(pages), secret=FIXTURE_SECRET)
        return result

    def run(self, key: str = "water-1", *, source_ids: list[str] | None = None, later: bool = False) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "water",
                   "max_results": 1000, "max_bytes": 20_000_000, "timeout_ms": 60_000,
                   "source_ids": source_ids or SOURCES}
        return self.runtime.run(request, principal_id="operator", adapters=self.adapters(later),
                                dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None)

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self

    def place(self, name: str, geometry: dict | None, *, place_type: str = "district",
              source_ids: dict | None = None) -> dict:
        from src.kb.geospatial import GeospatialStore

        return GeospatialStore(self.conn).register_place(
            NS, name, place_type, names=[{"value": name, "language": "de", "kind": "canonical"}],
            source_ids=source_ids or {"fixture": name.casefold()}, parent_ids=[], principal_id="alice",
            scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"}, geometry=geometry)

    def places(self) -> dict[str, str]:
        """The fixture places: two districts with boundaries, a river by its published identifier, an empty place."""
        return {
            "exampla": self.place("Exampla district (fixture)", EXAMPLA_DISTRICT)["place_id"],
            "county": self.place("Sample County (fixture)", SAMPLE_COUNTY)["place_id"],
            "nordfluss": self.place("Nordfluss (fixture)", None, place_type="river",
                                    source_ids={"pegelonline-water": "NORDFLUSS"})["place_id"],
            "moor": self.place("Empty Moor (fixture)", EMPTY_MOOR)["place_id"],
        }
