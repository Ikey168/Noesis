"""Offline harness for the Products safety feature (#1916): authored notice payloads through the real runtime.

Every payload under ``tests/fixtures/source_packs/products-safety-*.json`` is
authored in the provider's documented shape with fictional products,
companies and notice numbers; nothing here is live coverage. Notices go
through :class:`SafetyNoticeAdapter` (the connector the runtime compiles) and
:class:`ProductSafetyProjector`; Products identities come from the pinned
``products-displays`` display fixtures and the cited acts from the
``legal-research`` CELLAR fixture.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.product_sources import fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.product_safety import ProductSafetyStore

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/products.json"
LEGAL_PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
READ = {"knowledge:products:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:products:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:products:review"}
ALL = REVIEW | {
    "knowledge:standards:read",
    "knowledge:legal:read",
    "knowledge:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:entity-history:read",
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
}
DISPLAY_SOURCES = ["icecat-displays", "eprel-displays"]
NOTICE_SOURCES = [
    "safety-gate-alerts",
    "cpsc-recalls",
    "nhtsa-recalls",
    "rasff-notifications",
]
FIXTURES = {
    "safety-gate-alerts": ROOT
    / "tests/fixtures/source_packs/products-safety-safety-gate.json",
    "cpsc-recalls": ROOT / "tests/fixtures/source_packs/products-safety-cpsc.json",
    "nhtsa-recalls": ROOT / "tests/fixtures/source_packs/products-safety-nhtsa.json",
    "rasff-notifications": ROOT
    / "tests/fixtures/source_packs/products-safety-rasff.json",
}
FORBIDDEN = {
    "verdict",
    "safe",
    "unsafe",
    "safety_status",
    "risk_score",
    "recommendation",
    "advice",
    "consumer_advice",
    "is_safe",
}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub


def manifest(path: Path = PACK) -> dict:
    return validate_source_pack(json.loads(path.read_text()))


def source(source_id: str, value: dict | None = None) -> dict:
    value = value or manifest()
    return copy.deepcopy(
        next(s for s in value["sources"] if s["source_id"] == source_id)
    )


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURES[source_id].read_text())["native_pages"])


def alert_page(native: list[dict], number: str) -> dict:
    return next(
        p
        for p in native
        if p["request"].endswith("reference=" + number.replace("/", "%2F"))
    )


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
    """One in-memory deployment with the products and legal packs installed and licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(
            self.value, principal_id="operator", enable=True, now_ms=10
        )
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(
            self.conn, now=lambda: next(self.clock), sleep=lambda _d: None
        )
        for item in self.value["sources"]:
            self.runtime.accept_license(
                self.value["pack_id"], item["source_id"], principal_id="operator"
            )
        self.store = ProductSafetyStore(self.conn, now=lambda: next(self.clock))

    def run(
        self,
        key: str,
        *,
        operation: str,
        source_ids: list[str],
        adapters=None,
        fault=None,
        pack_id: str | None = None,
    ) -> dict:
        pack_id = pack_id or self.value["pack_id"]
        request = {
            "pack_id": pack_id,
            "run_key": key,
            "operation": operation,
            "max_results": 100,
            "max_bytes": 20_000_000,
            "timeout_ms": 60_000,
            "source_ids": source_ids,
        }
        return self.runtime.run(
            request,
            principal_id="operator",
            adapters=adapters
            if adapters is not None
            else self.runtime.fixture_adapters(pack_id, ROOT),
            dns_resolver=PUBLIC_DNS,
            secret_resolver=lambda _ref: "fixture-credential",
            fault=fault,
        )

    def run_displays(self, key: str = "displays-1") -> dict:
        return self.run(key, operation="models", source_ids=DISPLAY_SOURCES)

    def run_notices(
        self, key: str = "notices-1", *, adapters=None, source_ids=None, fault=None
    ) -> dict:
        return self.run(
            key,
            operation="notices",
            source_ids=source_ids or NOTICE_SOURCES,
            adapters=adapters,
            fault=fault,
        )

    def compiled(self, source_id: str, native_pages: list[dict]):
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        return self.runtime.factory.compile(
            source(source_id, installed), transport=fixture_transport(native_pages)
        )

    def load_legal(self) -> dict:
        legal = manifest(LEGAL_PACK)
        SourcePackStore(self.conn).install(
            legal, principal_id="operator", enable=True, now_ms=11
        )
        self.runtime.accept_license(
            legal["pack_id"], "cellar-product-safety-acts-eng", principal_id="operator"
        )
        return self.run(
            "legal-1",
            operation="records",
            source_ids=["cellar-product-safety-acts-eng"],
            pack_id=legal["pack_id"],
        )

    def seed_standard(self, reference: str, native_id: str) -> None:
        """One authored ISO catalogue edition (the shape the iso-open-data connector projects)."""
        from src.kb.standards import StandardsStore

        StandardsStore(self.conn).observe_page(
            NS,
            [
                {
                    "standard_record": {
                        "contract": "noesis-standard-catalogue-v1",
                        "provider": "iso-open-data",
                        "native_id": native_id,
                        "reference": reference,
                        "edition": 1,
                        "publication_date": "2017-02-01",
                        "status": "published",
                        "catalogue_url": f"https://www.iso.org/standard/{native_id}.html",
                        "content_access": "protected-link-only",
                        "native_sha256": "0" * 64,
                    }
                }
            ],
            run_id="standards-seed",
        )

    def loaded(self) -> Env:
        """Displays, notices, cited acts and one cited standard, with match proposals made."""
        assert self.run_displays()["status"] == "complete"
        assert self.run_notices()["status"] == "complete"
        assert self.load_legal()["status"] == "complete"
        self.seed_standard("ISO 6579-1:2017", "56712")
        self.store.propose_matches(NS, scopes=REVIEW, principal_id="matcher")
        self.store.link_citations(NS, scopes=ALL, principal_id="linker")
        return self

    def model(self, provider: str, designation: str) -> str:
        return self.conn.execute(
            "SELECT identity_id FROM product_identities WHERE namespace=? AND level='model' AND provider=? "
            "AND designation=?",
            [NS, provider, designation],
        ).fetchone()[0]

    def notice(self, provider: str, number: str) -> str:
        return self.store.resolve_notice(NS, f"{provider}:{number}")

    def candidate(self, notice_id: str, model_id: str) -> dict:
        return next(
            m
            for m in self.store.matches_for_notice(NS, notice_id)
            if m["model_id"] == model_id
        )

    def accept(
        self, notice_id: str, model_id: str, reason: str = "GTIN, brand and model agree"
    ) -> dict:
        match = self.candidate(notice_id, model_id)
        return self.store.review_match(
            NS,
            match["match_id"],
            "accepted",
            reason,
            scopes=REVIEW,
            principal_id="reviewer",
        )
