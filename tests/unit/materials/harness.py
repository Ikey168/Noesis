"""Offline harness: the materials source pack replayed through the source-pack runtime (MT04-MT07).

``config/source_packs/materials.json`` is installed and every source runs
through :class:`SourcePackRuntime` with its pinned fixture, the real
``materials`` adapter and :class:`src.kb.materials_store.MaterialsProjector`.
Later releases go through the same adapter with authored transports. All IDs
and values are fictional.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.materials_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from tests.unit.materials import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config/source_packs/materials.json"
PACK_ID = "materials"
NS = "materials"
READ = "knowledge:materials:read"
WRITE = "knowledge:materials:write"
SCOPES = {READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write"}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
SOURCES = {
    "materials-project": "materials-project-ti-al-oxides",
    "jarvis-dft": "jarvis-dft-3d-snapshot",
    "oqmd": "oqmd-ti-al-oxides",
    "nist-webbook": "nist-webbook-titanium-oxide",
    "cod": "cod-titanium-oxide-structures",
}


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(CONFIG.read_text()))


def source(source_id: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == source_id)


class Env:
    def __init__(self, conn=None, start_ms: int = 4_073_600_000_000) -> None:
        self.conn = conn or duckdb.connect(":memory:")
        self.clock = start_ms  # 2099-02-01

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def install(self) -> Env:
        SourcePackStore(self.conn).install(
            manifest(), principal_id="operator", enable=True, now_ms=1
        )
        runtime = self.runtime()
        for source_id in SOURCES.values():
            runtime.accept_license(PACK_ID, source_id, principal_id="operator")
        return self

    def run(
        self, source_ids, key: str, *, adapters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, ROOT)
        selected = {s: (adapters or {}).get(s) or fixtures[s] for s in source_ids}
        return runtime.run(
            {
                "pack_id": PACK_ID,
                "run_key": key,
                "operation": "properties",
                "source_ids": list(source_ids),
                "max_results": 1000,
                "max_bytes": 50_000_000,
                "timeout_ms": 120_000,
            },
            principal_id="operator",
            adapters=selected,
            dns_resolver=PUBLIC_DNS,
            secret_resolver=lambda _ref: FIXTURE_SECRET,
        )

    def adapter(self, source_id: str, pages):
        return self.runtime().factory.compile(
            source(source_id), transport=fixture_transport(pages), secret=FIXTURE_SECRET
        )

    def load(self) -> Env:
        """Every source replayed from its pinned fixture."""

        self.install()
        self.run(list(SOURCES.values()), "materials-fixtures")
        return self

    def mp_release_2(self, key: str = "mp-2099.2.0") -> dict[str, Any]:
        adapter = self.adapter(SOURCES["materials-project"], fb.mp_pages(release=2))
        return self.run(
            [SOURCES["materials-project"]],
            key,
            adapters={SOURCES["materials-project"]: adapter},
        )
