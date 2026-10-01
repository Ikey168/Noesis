"""Offline harness for the Climate and Environment water features (#2582): authored payloads, real runtime.

Payloads under ``tests/fixtures/source_packs/water-*.json`` (built by
:mod:`tests.unit.water.fixture_builder`) are authored in each provider's
documented response shape; identifiers, coordinates, values and status labels
are illustrative. They run through :class:`WaterSourceAdapter` (the connector
the runtime compiles) and :class:`WaterProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.water_sources import FIXTURE_SECRET, fixture_transport
from src.kb.water_store import WaterStore

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
}
SOURCES = ["pegelonline-stations-levels", "usgs-water-data", "eea-wise-wfd-status", "eea-wise-water-body-geometry"]
LATER = json.loads((ROOT / "tests/fixtures/water/later_payloads.json").read_text())
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
DRESDEN = {"type": "Polygon", "coordinates": [[[13.60, 50.98], [13.95, 50.98], [13.95, 51.15], [13.60, 51.15],
                                               [13.60, 50.98]]]}
MONTGOMERY = {"type": "Polygon", "coordinates": [[[-77.53, 38.93], [-76.88, 38.93], [-76.88, 39.35],
                                                  [-77.53, 39.35], [-77.53, 38.93]]]}
FORBIDDEN = {"forecast", "predicted", "prediction", "interpolated", "gap_filled", "filled_value", "resampled",
             "flood_risk", "risk_score", "flood_probability", "derived_status", "noesis_status"}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def forbidden_keys(value: Any) -> set[str]:
    from src.kb.water_records import PERSONAL_KEYS

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN | PERSONAL_KEYS:
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
        self.clock = 1_790_000_000_000  # 2026-09-21T13:33Z
        self.runtime = SourcePackRuntime(self.conn, now=self.tick, sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = WaterStore(self.conn, now=self.tick)

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
                   "max_results": 500, "max_bytes": 20_000_000, "timeout_ms": 60_000,
                   "source_ids": source_ids or SOURCES}
        # The runtime builds the projector's store without a clock; retrieval times follow this deployment's clock.
        clock = SimpleNamespace(time=lambda: self.tick() / 1000)
        with mock.patch("src.kb.water_store.time", clock):
            return self.runtime.run(request, principal_id="operator", adapters=self.adapters(later),
                                    dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self

    def place(self, name: str, geometry: dict | None, *, place_type: str = "district",
              source_ids: dict | None = None) -> dict:
        from src.kb.geospatial import GeospatialStore

        return GeospatialStore(self.conn).register_place(
            NS, name, place_type, names=[{"value": name, "language": "und", "kind": "canonical"}],
            source_ids=source_ids or {"fixture": name.casefold()}, parent_ids=[], principal_id="alice",
            scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"}, geometry=geometry)

    def places(self) -> dict[str, str]:
        """Dresden (boundary), the Elbe (river, by PEGELONLINE water id), Montgomery County (boundary and FIPS)."""
        return {
            "dresden": self.place("Dresden (fixture)", DRESDEN)["place_id"],
            "elbe": self.place("Elbe (fixture)", None, place_type="river",
                               source_ids={"pegelonline-water": "ELBE"})["place_id"],
            "montgomery": self.place("Montgomery County (fixture)", MONTGOMERY,
                                     source_ids={"us-county-fips": "24031"})["place_id"],
            "nowhere": self.place("Empty district (fixture)", {"type": "Polygon", "coordinates": [[
                [10.0, 48.0], [10.1, 48.0], [10.1, 48.1], [10.0, 48.1], [10.0, 48.0]]]})["place_id"],
        }


# ------------------------------------------------------------------ other packs' records (synthetic, offline)


def seed_hazard(conn: Any, *, cited: str) -> dict[str, str]:
    """A fictional flood notification whose published text cites a water identifier (natural-hazards tables)."""
    from src.kb.hazards_store import HazardStore

    HazardStore(conn)
    record = {"record_id": "hazard:fixture-flood-0001", "revision_id": "hazard-revision:fixture-flood-0001-1"}
    content = {"provider": "glofas", "native_id": "fixture-flood-0001", "hazard_type": "flood",
               "summary": f"Fictional flood notification referring to gauge {cited} (fixture)."}
    conn.execute("INSERT INTO hazard_records VALUES (?,?,?,?,?,?,?,?)",
                 [record["record_id"], NS, "alert", "glofas", "fixture-flood-0001", "flood", None, 1])
    conn.execute("INSERT INTO hazard_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [record["revision_id"], record["record_id"], NS, 1, "k1", "sha-fixture", json.dumps(content),
                  1, "publisher", 1, None, "run-fixture", "{}", "operator"])
    return record


def seed_weather_identifier(conn: Any, *, scheme: str, value: str) -> str:
    """A fictional weather station statement naming a water station's published identifier."""
    from src.kb.weather_store import WeatherStore

    WeatherStore(conn)
    station = "fixture-weather:station-0001"
    conn.execute("INSERT INTO weather_station_identifiers VALUES (?,?,?,?,?,?,?,?)",
                 [NS, station, scheme, value, "fixture-weather-source", json.dumps({"field": "co_located_gauge"}),
                  1, "run-fixture"])
    return station


def seed_infrastructure_asset(conn: Any, *, scheme: str, value: str) -> dict[str, str]:
    """A fictional hydropower asset whose published identifiers include a water identifier."""
    from src.kb.infrastructure_assets import InfrastructureStore

    InfrastructureStore(conn)
    asset = {"asset_id": "infra-asset:fixture-hydro-0001", "revision_id": "infra-revision:fixture-hydro-0001-1"}
    conn.execute("INSERT INTO infra_assets VALUES (?,?,?,?,?,?,?)",
                 [asset["asset_id"], NS, "gppd", "fixture", "HYDRO-0001", "power_plant", 1])
    record = {"name": "Fictional run-of-river plant (fixture)",
              "identifiers": [{"scheme": scheme, "value": value}, {"scheme": "gppd", "value": "HYDRO-0001"}]}
    conn.execute("INSERT INTO infra_asset_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [asset["revision_id"], asset["asset_id"], NS, 1, "fixture-release", "declared_release", 1, 1, 1,
                  "sha-fixture", json.dumps(record), None, None, None, 1])
    return asset
