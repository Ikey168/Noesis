"""Offline harness for the OSINT movements feature (#2221): authored payloads through the real runtime.

Every payload under ``tests/fixtures/source_packs/osint-movements-*.json`` is
authored in the provider's documented or observed shape. Every registration,
ICAO address, IMO number, MMSI, owner, airport and port is SYNTHETIC and every
position is illustrative, not live evidence (live evidence is #2291).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.osint_movement_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/osint.json"
NS = "osint"
SOURCES = ["movements-faa-registry", "movements-uk-caa-ginfo", "movements-opensky", "movements-gfw-port-visits",
           "movements-kystdatahuset-ais", "movements-unctad-port-calls"]
FIXTURES = {
    "movements-faa-registry": ROOT / "tests/fixtures/source_packs/osint-movements-faa.json",
    "movements-uk-caa-ginfo": ROOT / "tests/fixtures/source_packs/osint-movements-ginfo.json",
    "movements-opensky": ROOT / "tests/fixtures/source_packs/osint-movements-opensky.json",
    "movements-gfw-port-visits": ROOT / "tests/fixtures/source_packs/osint-movements-gfw.json",
    "movements-kystdatahuset-ais": ROOT / "tests/fixtures/source_packs/osint-movements-ais.json",
    "movements-unctad-port-calls": ROOT / "tests/fixtures/source_packs/osint-movements-unctad.json",
}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
FAA_PATH = "/AircraftInquiry/Search/NNumberResult"
VESSEL = "a1b2c3d4-0002-4000-8000-000000000002"
IMO, MMSI_OLD, MMSI_NEW = "9000027", "627000002", "671000002"
# Scopes a reviewer holding every capability the journey touches would have.
ALL = {
    "knowledge:read", "knowledge:osint:movements", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ownership:review",
    "knowledge:sanctions:read", "namespace:legal:read", "knowledge:fisheries:read", "namespace:global:read",
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:entity-history:read",
}


def manifest(path: Path = PACK) -> dict:
    return validate_source_pack(json.loads(path.read_text()))


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURES[source_id].read_text())["native_pages"])


def faa_request(mark: str) -> str:
    return f"{FAA_PATH}?nNumberTxt={mark}"


class Env:
    """One in-memory deployment with the OSINT source pack installed and the movement licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for source_id in SOURCES:
            self.runtime.accept_license(self.value["pack_id"], source_id, principal_id="operator")

    def adapters(self, overrides: dict[str, dict[str, Any]] | None = None) -> dict:
        """Fixture adapters; ``overrides`` replaces the body (and status) of named requests (a later acquisition)."""
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        result = {}
        for source in installed["sources"]:
            if source["source_id"] not in FIXTURES:
                continue
            native = pages(source["source_id"])
            for item in native:
                if item["request"] in (overrides or {}):
                    item.update(copy.deepcopy(overrides[item["request"]]))
            result[source["source_id"]] = self.runtime.factory.compile(
                source, transport=fixture_transport(native), secret=FIXTURE_SECRET)
        return result

    def run(self, key: str = "movements-1", *, source_ids: list[str] | None = None, overrides: dict | None = None
            ) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "movements", "max_results": 1000,
                   "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": source_ids or SOURCES}
        return self.runtime.run(request, principal_id="operator", adapters=self.adapters(overrides),
                                dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self


def load_fisheries_gfw(conn: Any) -> None:
    """Load only the Fisheries pack's GFW vessel-identity source (identity is shared by citation, #2258)."""
    from tests.unit.fisheries.harness import Env as FisheriesEnv

    result = FisheriesEnv(conn).run("fisheries-gfw", source_ids=["gfw-vessels-effort"])
    assert result["status"] == "complete", result


def load_fisheries_all(conn: Any) -> None:
    """Load every Fisheries source (GFW identity, registers and IUU lists) for citation links (#2276)."""
    from tests.unit.fisheries.harness import Env as FisheriesEnv

    FisheriesEnv(conn).loaded()


def seed_sanctions(conn: Any, *, delisted: bool = False) -> None:
    """Authored OFAC-shaped snapshots: a vessel stating IMO 9000027, an aircraft stating N901EX, a name-only entry.

    ``delisted`` adds a later snapshot without the vessel entry (a delisting revision).
    """
    from src.kb.sanctions import SanctionsStore

    def entry(entry_id, kind, name, identifiers):
        return {"list_id": "ofac", "entry_id": entry_id, "party_kind": kind,
                "names": [{"name": name, "kind": "primary", "quality": None, "script": None, "language": None}],
                "identifiers": identifiers, "addresses": [], "programmes": [], "legal_basis": [],
                "dates_of_birth": [], "nationalities": [], "remarks": [], "listed_on": "2025-02-01",
                "amended_on": None, "cross_references": {}}

    vessel = entry("SYN-MV-1", "vessel", "SAMPLE NOVA", [
        {"kind": "imo", "value": IMO, "source_type": "Vessel Registration Identification", "country": None,
         "note": None}])
    aircraft = entry("SYN-MV-2", "aircraft", "EXAMPLE JET", [
        {"kind": "other", "value": "N901EX", "source_type": "Aircraft Tail Number", "country": None, "note": None}])
    named = entry("SYN-MV-3", "vessel", "SAMPLE STAR", [])
    store = SanctionsStore(conn)
    entries = [vessel, aircraft, named]
    store.apply_snapshot("legal", {"list_id": "ofac", "publication_date": "2025-03-01", "file_sha256": "a" * 64,
                                   "entry_count": len(entries), "format": "fixture"}, entries,
                         run_id="sanctions-1", source_id=None)
    if delisted:
        entries = [aircraft, named]
        store.apply_snapshot("legal", {"list_id": "ofac", "publication_date": "2025-09-01", "file_sha256": "b" * 64,
                                       "entry_count": len(entries), "format": "fixture"}, entries,
                             run_id="sanctions-2", source_id=None)


def facilities(conn: Any) -> dict[str, str]:
    """Fictional airport and port polygons in the geospatial store; returns place ids by name."""
    from src.kb.geospatial import GeospatialStore

    store = GeospatialStore(conn, now=lambda: 5)
    scopes = {"knowledge:geospatial:write"}
    places = {}
    for name, kind, code, lon, lat, half in (("Example Field A", "airport", "KEXA", -100.0, 40.0, 0.02),
                                             ("Example Field B", "airport", "KEXB", -99.0, 41.0, 0.02),
                                             ("Example Harbour", "port", "NOEXH", 5.30, 60.39, 0.01),
                                             ("Example Bay", "port", "NOEXB", 5.10, 59.40, 0.01)):
        ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
                [lon - half, lat + half], [lon - half, lat - half]]
        placed = store.register_place(
            "movements-facilities", name, kind, names=[{"value": name, "language": "en", "kind": "canonical"}],
            source_ids={"code": code}, parent_ids=[], principal_id="operator", scopes=scopes,
            place_key=f"{kind}:{code}", geometry={"type": "Polygon", "coordinates": [ring]})
        places[name] = placed["place_id"]
    return places
