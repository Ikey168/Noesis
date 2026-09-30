"""Offline harness for the Fisheries and Maritime Activity pack (#2222): authored payloads through the real runtime.

Every payload under ``tests/fixtures/source_packs/fisheries-*.json`` is
authored in the provider's documented or observed export shape. Every vessel,
IMO number, call sign, owner and listing is SYNTHETIC; statistics and effort
values are illustrative, not live evidence. Payloads go through
:class:`FisheriesSourceAdapter` (the connector the runtime compiles) and
:class:`FisheriesProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.fisheries_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.fisheries_store import FisheriesStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/fisheries.json"
NS = "global"
READ = {"knowledge:fisheries:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:fisheries:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:fisheries:review"}
ALL = REVIEW | {
    "knowledge:sanctions:read", "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:read",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:entity-history:read",
    "knowledge:entity-history:write", "knowledge:entity-history:review", "knowledge:entity-history:execute",
}
SOURCES = ["gfw-vessels-effort", "fao-fishstat-capture", "iccat-vessel-lists", "wcpfc-vessel-lists",
           "iotc-vessel-lists", "combined-iuu-vessel-list"]
FIXTURES = {
    "gfw-vessels-effort": ROOT / "tests/fixtures/source_packs/fisheries-gfw.json",
    "fao-fishstat-capture": ROOT / "tests/fixtures/source_packs/fisheries-fishstat.json",
    "iccat-vessel-lists": ROOT / "tests/fixtures/source_packs/fisheries-iccat.json",
    "wcpfc-vessel-lists": ROOT / "tests/fixtures/source_packs/fisheries-wcpfc.json",
    "iotc-vessel-lists": ROOT / "tests/fixtures/source_packs/fisheries-iotc.json",
    "combined-iuu-vessel-list": ROOT / "tests/fixtures/source_packs/fisheries-combined-iuu.json",
}
LATER = json.loads((ROOT / "tests/fixtures/fisheries/later_payloads.json").read_text())
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
IMO_CLEAN, IMO_REFLAGGED, IMO_DELISTED, IMO_LATE, IMO_MALFORMED = (
    "9000015", "9000027", "9000039", "9000065", "9000016")
FORBIDDEN = {"illegal", "is_illegal", "legal_status", "iuu_status", "compliant", "is_compliant", "verdict",
             "enforcement", "recommendation", "risk_score", "confirmed_fishing"}


def manifest(path: Path = PACK) -> dict:
    return validate_source_pack(json.loads(path.read_text()))


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURES[source_id].read_text())["native_pages"])


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


def seed_sanctions(conn: Any, imo: str = IMO_DELISTED, *, name: str = "SAMPLE DRIFTER") -> None:
    """One authored OFAC-shaped vessel designation stating an IMO number, stored by the sanctions owner."""
    from src.kb.sanctions import SanctionsStore

    entry = {"list_id": "ofac", "entry_id": "SYN-90001", "party_kind": "vessel",
             "names": [{"name": name, "kind": "primary", "quality": None, "script": None, "language": None}],
             "identifiers": [{"kind": "imo", "value": f"IMO {imo}", "source_type": "Vessel Registration "
                              "Identification", "country": None, "note": None}],
             "addresses": [], "programmes": [], "legal_basis": [], "dates_of_birth": [], "nationalities": [],
             "remarks": [], "listed_on": "2025-02-01", "amended_on": None, "cross_references": {}}
    other = {**entry, "entry_id": "SYN-90002", "names": [{"name": "SAMPLE ALBACORA UNO", "kind": "primary",
                                                          "quality": None, "script": None, "language": None}],
             "identifiers": []}
    SanctionsStore(conn).apply_snapshot(
        NS, {"list_id": "ofac", "publication_date": "2026-03-01", "file_sha256": "e" * 64, "entry_count": 2,
             "format": "fixture"}, [entry, other], run_id="sanctions-seed", source_id=None)


class Env:
    """One in-memory deployment with the fisheries source pack installed and its licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = FisheriesStore(self.conn, now=lambda: next(self.clock))

    def adapters(self, overrides: dict[str, dict[str, Any]] | None = None) -> dict:
        """Fixture adapters; ``overrides`` replaces the body (and headers) of named requests (a later acquisition)."""
        if not overrides:
            return self.runtime.fixture_adapters(self.value["pack_id"], ROOT)
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        result = {}
        for source in installed["sources"]:
            native = pages(source["source_id"])
            for item in native:
                if item["request"] in overrides:
                    item.update(copy.deepcopy(overrides[item["request"]]))
            result[source["source_id"]] = self.runtime.factory.compile(
                source, transport=fixture_transport(native), secret=FIXTURE_SECRET)
        return result

    def run(self, key: str = "fisheries-1", *, source_ids: list[str] | None = None, adapters=None,
            overrides: dict | None = None) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "fisheries", "max_results": 1000,
                   "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": source_ids or SOURCES}
        if adapters is None:
            adapters = self.adapters(overrides)
        return self.runtime.run(request, principal_id="operator", adapters=adapters, dns_resolver=PUBLIC_DNS,
                                secret_resolver=lambda _ref: "fixture-credential")

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self


_TEMPLATE: dict[str, Path] = {}


def loaded_env(directory: Path) -> Env:
    """An Env over a copy of one database loaded once per session with every fisheries fixture."""
    import shutil
    import tempfile

    if "path" not in _TEMPLATE:
        path = Path(tempfile.mkdtemp(prefix="fisheries-template-")) / "loaded.duckdb"
        conn = duckdb.connect(str(path))
        Env(conn).loaded()
        conn.close()
        _TEMPLATE["path"] = path
    target = Path(directory) / "fisheries.duckdb"
    shutil.copy(_TEMPLATE["path"], target)
    env = Env(duckdb.connect(str(target)))
    env.clock = iter(range(10_000_000, 10_000_000_000, 1_000))  # after every template timestamp
    return env
