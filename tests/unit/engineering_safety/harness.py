"""Offline harness for the Engineering Safety pack (#2059): authored fixtures through the real runtime.

Every payload under ``tests/fixtures/source_packs/engineering-safety-*.json`` and
``tests/fixtures/engineering_safety/variants.json`` is authored in the provider's
documented shape with fictional manufacturers, operators, AD numbers, N-numbers,
dockets and report numbers; nothing here is live coverage. Records go through
:class:`EngineeringSafetyAdapter` (the connector the runtime compiles) and
:class:`EngineeringSafetyProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.engineering_safety_sources import fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.engineering_safety_store import EngineeringSafetyStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/engineering-safety.json"
VARIANTS = ROOT / "tests/fixtures/engineering_safety/variants.json"
NS = "global"
READ_SCOPE = "knowledge:engineering-safety:read"
WRITE_SCOPE = "knowledge:engineering-safety:write"
REVIEW_SCOPE = "knowledge:engineering-safety:review"
READ = {READ_SCOPE, f"namespace:{NS}:read"}
WRITE = READ | {WRITE_SCOPE, f"namespace:{NS}:write"}
REVIEW = WRITE | {REVIEW_SCOPE}
ALL = REVIEW | {
    "knowledge:standards:read",
    "knowledge:legal:read",
    "knowledge:products:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
}
SOURCES = [
    "faa-airworthiness-directives",
    "easa-airworthiness-directives",
    "ntsb-investigations",
    "phmsa-hazardous-liquid-incidents",
    "csb-investigations",
    "nhtsa-odi-investigations",
    "nhtsa-complaints",
    "bfu-reports",
    "bea-reports",
]
FORBIDDEN = {"verdict", "safe", "unsafe", "is_safe", "airworthy", "compliant", "risk_score", "ranking",
             "safety_rating", "cause_inferred"}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
EX100 = {"kind": "aircraft_model", "model": "EX-100"}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str, value: dict | None = None) -> dict:
    value = value or manifest()
    return copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == source_id))


def fixture(source_id: str) -> dict:
    return json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(fixture(source_id)["native_pages"])


def variants() -> dict:
    return json.loads(VARIANTS.read_text())


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
    """One in-memory deployment with the engineering-safety pack installed and every licence accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = EngineeringSafetyStore(self.conn, now=lambda: next(self.clock))

    def compiled(self, source_id: str, native_pages: list[dict]):
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        return self.runtime.factory.compile(source(source_id, installed), transport=fixture_transport(native_pages))

    def run(self, key: str, *, source_ids: list[str] | None = None, overrides: dict[str, list[dict]] | None = None,
            fault=None) -> dict:
        """Run the named sources; ``overrides`` serves other native pages for a source (a later publication)."""
        adapters = self.runtime.fixture_adapters(self.value["pack_id"], ROOT)
        for source_id, native in (overrides or {}).items():
            adapters[source_id] = self.compiled(source_id, native)
        selected = source_ids or SOURCES
        return self.runtime.run(
            {"pack_id": self.value["pack_id"], "run_key": key, "operation": "records", "max_results": 1000,
             "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": selected},
            principal_id="operator", adapters={k: v for k, v in adapters.items() if k in selected},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None, fault=fault)

    def record(self, provider: str, native_id: str, kind: str | None = None) -> str:
        return self.store.resolve(NS, f"{provider}:{native_id}", kind)


def ntsb_pages(case: dict | None = None, statuses: list[dict] | None = None) -> list[dict]:
    native = pages("ntsb-investigations")
    for page in native:
        if case is not None and page["request"].endswith("/cases/ERA26FA101"):
            page["body"] = case
        if statuses is not None and page["request"].endswith("/recommendations/A-26-015"):
            page["body"] = {**page["body"], "StatusHistory": statuses}
    return native


def odi_pages(body: str) -> list[dict]:
    native = pages("nhtsa-odi-investigations")
    native[0]["body"] = body
    return native


def seed_products(conn: Any) -> str:
    """One Products model (the Products display pack shape) named VELOMARK CITYRUNNER; returns its model id."""
    conn.execute("""CREATE TABLE IF NOT EXISTS product_identities (
      identity_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, level TEXT NOT NULL,
      parent_id TEXT, provider TEXT, brand TEXT, designation TEXT, family TEXT,
      provider_record_id TEXT, market_json TEXT NOT NULL, identifiers_json TEXT NOT NULL,
      category_label TEXT, created_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL)""")
    rows = [("product-model:velomark-cityrunner", "VELOMARK", "CITYRUNNER"),
            ("product-model:velomark-cityrunner-x", "VELOMARK", "CITYRUNNER X"),
            ("product-model:otherbrand-cityrunner", "OTHERBRAND", "CITYRUNNER")]
    for identity, brand, designation in rows:
        conn.execute("INSERT OR IGNORE INTO product_identities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [identity, NS, "model", None, "fixture", brand, designation, None, identity, "{}", "{}",
                      "vehicles", "seed", 1])
    return rows[0][0]


def seed_entities(conn: Any) -> dict[str, str]:
    """Canonical entities with aliases for two published organisation names; returns name -> canonical id."""
    from src.kb.entities import ensure_entity_schema, normalize_surface

    ensure_entity_schema(conn)
    result = {}
    for name, canonical_id in (("Examplar Pipeline Company LP", "ent-examplar-pipeline"),
                               ("Skyline Charter LLC", "ent-skyline-charter")):
        conn.execute("INSERT OR IGNORE INTO canonical_entities VALUES (?,?,?,?)", [canonical_id, name, "ORG", 1])
        conn.execute("INSERT OR IGNORE INTO entity_aliases VALUES (?,?,?,?,?,?)",
                     [normalize_surface(name), canonical_id, 1.0, "exact", None, 1])
        result[name] = canonical_id
    return result


def seed_recall(conn: Any) -> str:
    """The Products safety feature's NHTSA campaign 26V104000 (its own fixture), applied through its store."""
    from src.ingestion.product_sources import parse_nhtsa_campaign
    from src.kb.product_safety import ProductSafetyStore

    products = json.loads((ROOT / "tests/fixtures/source_packs/products-safety-nhtsa.json").read_text())
    rows = next(p for p in products["native_pages"] if p["request"].endswith("26V104000"))["body"]["results"]
    store = ProductSafetyStore(conn)
    return store.apply(NS, parse_nhtsa_campaign(rows))["notice_id"]
