"""Offline harness for the Chemicals and Substances pack (#2212): authored payloads through the real runtime.

Every payload under ``tests/fixtures/source_packs/chemicals-*.json`` is
authored in the provider's documented (PubChem, CTX) or observed (ECHA CHEM)
response shape; identifiers are the substances' public identifiers, and
classification, list and data-point values are illustrative, not live
evidence. Payloads go through :class:`SubstanceSourceAdapter` (the connector
the runtime compiles) and :class:`SubstanceProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.substance_sources import FIXTURE_SECRET, fixture_transport
from src.kb.substances_store import SubstanceStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/chemicals.json"
LEGAL_PACK = ROOT / "config/source_packs/legal.json"
PRODUCTS_PACK = ROOT / "config/source_packs/products.json"
NS = "global"
READ = {"knowledge:substances:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:substances:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:substances:review"}
ALL = REVIEW | {
    "knowledge:legal:read", "knowledge:products:read", "knowledge:read", "knowledge:subscriptions:read",
    "knowledge:subscriptions:write", "knowledge:entity-history:read", "knowledge:entity-history:write",
    "knowledge:entity-history:review", "knowledge:entity-history:execute",
}
SOURCES = ["pubchem-compounds", "echa-clp-classifications", "echa-reach-lists", "comptox-toxval"]
FIXTURES = {
    "pubchem-compounds": ROOT / "tests/fixtures/source_packs/chemicals-pubchem.json",
    "echa-clp-classifications": ROOT / "tests/fixtures/source_packs/chemicals-echa-clp.json",
    "echa-reach-lists": ROOT / "tests/fixtures/source_packs/chemicals-echa-reach.json",
    "comptox-toxval": ROOT / "tests/fixtures/source_packs/chemicals-comptox.json",
}
LATER = json.loads((ROOT / "tests/fixtures/chemicals/later_payloads.json").read_text())
NOTICES = json.loads((ROOT / "tests/fixtures/chemicals/product_notices.json").read_text())["alerts"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
FORBIDDEN = {"verdict", "safe", "unsafe", "is_safe", "hazard_score", "risk_score", "safety_advice", "advice",
             "recommendation", "synthesis", "preparation", "exposure_assessment"}


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
    """One in-memory deployment with the chemicals source pack installed and its licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = SubstanceStore(self.conn, now=lambda: next(self.clock))

    def adapters(self, overrides: dict[str, dict[str, Any]] | None = None) -> dict:
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

    def run(self, key: str = "chemicals-1", *, source_ids: list[str] | None = None, adapters=None,
            overrides: dict | None = None, pack_id: str | None = None, operation: str = "substances") -> dict:
        pack_id = pack_id or self.value["pack_id"]
        request = {"pack_id": pack_id, "run_key": key, "operation": operation, "max_results": 1000,
                   "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": source_ids or SOURCES}
        if adapters is None:
            adapters = (self.adapters(overrides) if pack_id == self.value["pack_id"]
                        else self.runtime.fixture_adapters(pack_id, ROOT))
        return self.runtime.run(request, principal_id="operator", adapters=adapters, dns_resolver=PUBLIC_DNS,
                                secret_resolver=lambda _ref: "fixture-credential")

    def load_legal(self) -> dict:
        legal = manifest(LEGAL_PACK)
        SourcePackStore(self.conn).install(legal, principal_id="operator", enable=True, now_ms=11)
        self.runtime.accept_license(legal["pack_id"], "cellar-product-safety-acts-eng", principal_id="operator")
        return self.run("legal-1", source_ids=["cellar-product-safety-acts-eng"], pack_id=legal["pack_id"],
                        operation="records")

    def seed_legal_act(self, celex: str, title: str, passages: list[tuple[str, str]] = ()) -> str:
        """One authored EU act in the Legal owner's CELLAR record shape, with passages at the given paths."""
        from src.kb.legal import LegalStore

        store = LegalStore(self.conn)
        record = {"contract": "noesis-native-regional-v1", "provider": "cellar", "provider_id": celex,
                  "kind": "legislation", "language": "en", "title": title,
                  "fields": {"celex": celex, "work": f"cellar-work:{celex}"},
                  "sections": [{"locator": {"path": path}, "text": text} for path, text in passages]}
        store.project(NS, [record], run_id="legal-seed", source_id="legal-seed")
        return store.lookup(NS, scopes={"knowledge:legal:read", f"namespace:{NS}:read"}, identifier=celex)[
            "works"][0]["work_id"]

    def seed_notices(self) -> list[str]:
        """Authored Safety Gate alerts parsed and stored by the Products pack's own parser and store."""
        from src.ingestion.product_sources import parse_safety_gate_alert
        from src.kb.product_safety import ProductSafetyStore

        store = ProductSafetyStore(self.conn, now=lambda: next(self.clock))
        return [store.apply(NS, parse_safety_gate_alert(copy.deepcopy(alert)))["notice_id"] for alert in NOTICES]

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self
