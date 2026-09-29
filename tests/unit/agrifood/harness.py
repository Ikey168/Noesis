"""Offline harness for the Agriculture and Food Systems pack (#2213): authored payloads through the real runtime.

Every payload under ``tests/fixtures/source_packs/agrifood-*.json`` is authored
in the provider's documented response shape; codes are the publishers' public
codes and every figure, flag and release date is illustrative, not live
evidence. Payloads go through :class:`AgrifoodSourceAdapter` (the connector the
runtime compiles) and :class:`AgrifoodProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.agrifood_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.agrifood_store import AgrifoodStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/agrifood.json"
NS = "global"
READ = {"knowledge:agrifood:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:agrifood:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:agrifood:review"}
GEO = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:schema:read", "knowledge:schema:register"}
ALL = REVIEW | GEO | {
    "knowledge:products:read", "knowledge:environment:read", "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
SOURCES = ["faostat-qcl-pp-fbs", "nass-quickstats-crops", "fas-psd-balances", "eurostat-agri-crops-prices",
           "agri-food-portal-cereal-prices"]
FIXTURES = {
    "faostat-qcl-pp-fbs": ROOT / "tests/fixtures/source_packs/agrifood-faostat.json",
    "nass-quickstats-crops": ROOT / "tests/fixtures/source_packs/agrifood-nass-quickstats.json",
    "fas-psd-balances": ROOT / "tests/fixtures/source_packs/agrifood-fas-psd.json",
    "eurostat-agri-crops-prices": ROOT / "tests/fixtures/source_packs/agrifood-eurostat-agri.json",
    "agri-food-portal-cereal-prices": ROOT / "tests/fixtures/source_packs/agrifood-agri-food-portal.json",
}
LATER = json.loads((ROOT / "tests/fixtures/agrifood/later_payloads.json").read_text())["payloads"]
RASFF = json.loads((ROOT / "tests/fixtures/agrifood/rasff_notices.json").read_text())["notifications"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
FORBIDDEN = {"forecast_by_pack", "projection_by_pack", "food_security_score", "score", "risk_score", "blended",
             "imputed_by_pack", "verdict"}


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


class Env:
    """One in-memory deployment with the agrifood source pack installed and its licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = AgrifoodStore(self.conn, now=lambda: next(self.clock))

    def tick(self) -> int:
        return next(self.clock)

    def adapters(self, overrides: dict[str, Any] | None = None) -> dict:
        """Fixture adapters; ``overrides`` replaces the body of named requests (a later acquisition)."""
        if not overrides:
            return self.runtime.fixture_adapters(self.value["pack_id"], ROOT)
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        result = {}
        for source in installed["sources"]:
            native = pages(source["source_id"])
            for item in native:
                if item["request"] in overrides:
                    item["body"] = copy.deepcopy(overrides[item["request"]])
            result[source["source_id"]] = self.runtime.factory.compile(
                source, transport=fixture_transport(native), secret=FIXTURE_SECRET)
        return result

    def run(self, key: str = "agrifood-1", *, source_ids: list[str] | None = None, adapters=None,
            overrides: dict | None = None) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "series", "max_results": 1000,
                   "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": source_ids or SOURCES}
        return self.runtime.run(request, principal_id="operator", adapters=adapters or self.adapters(overrides),
                                dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: "fixture-credential")

    def later(self, key: str = "agrifood-2") -> dict:
        return self.run(key, overrides=LATER)

    def seed_rasff(self) -> list[str]:
        """Authored RASFF notifications parsed and stored by the Products pack's own parser and store."""
        from src.ingestion.product_sources import parse_rasff_notification
        from src.kb.product_safety import ProductSafetyStore

        store = ProductSafetyStore(self.conn, now=lambda: next(self.clock))
        return [store.apply(NS, parse_rasff_notification(copy.deepcopy(item)))["notice_id"] for item in RASFF]

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self


_TEMPLATE: dict[str, Path] = {}


def loaded_env(directory: Path) -> Env:
    """An Env over a copy of one database loaded once per session with every agrifood fixture."""
    import shutil
    import tempfile

    if "path" not in _TEMPLATE:
        path = Path(tempfile.mkdtemp(prefix="agrifood-template-")) / "loaded.duckdb"
        conn = duckdb.connect(str(path))
        Env(conn).loaded()
        conn.close()
        _TEMPLATE["path"] = path
    target = Path(directory) / "agrifood.duckdb"
    shutil.copy(_TEMPLATE["path"], target)
    env = Env(duckdb.connect(str(target)))
    env.clock = iter(range(10_000_000, 10_000_000_000, 1_000))  # after every template timestamp
    return env
