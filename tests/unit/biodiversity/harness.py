"""Offline harness for the Climate and Environment biodiversity feature (#2220): authored payloads, real runtime.

Payloads under ``tests/fixtures/source_packs/biodiversity-*.json`` (built by
:mod:`tests.unit.biodiversity.fixture_builder`) are authored in each provider's
documented response shape; identifiers, DOIs, coordinates and categories are
illustrative. They run through :class:`BiodiversitySourceAdapter` (the connector
the runtime compiles) and :class:`BiodiversityProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.biodiversity_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.biodiversity_store import BiodiversityStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-biodiversity.json"
NS = "environment"
READ = {"knowledge:environment:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:environment:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:environment:review"}
ALL = REVIEW | {
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:entity-history:read",
    "knowledge:entity-history:write", "knowledge:entity-history:review", "knowledge:entity-history:execute",
}
SOURCES = ["col-checklist-releases", "gbif-species-occurrences", "iucn-red-list-reference"]
LATER = json.loads((ROOT / "tests/fixtures/biodiversity/later_payloads.json").read_text())
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
MITTE = {"type": "Polygon", "coordinates": [[[13.36, 52.50], [13.43, 52.50], [13.43, 52.54], [13.36, 52.54],
                                             [13.36, 52.50]]]}
BERLIN = {"type": "Polygon", "coordinates": [[[13.08, 52.33], [13.76, 52.33], [13.76, 52.68], [13.08, 52.68],
                                              [13.08, 52.33]]]}
FORBIDDEN = {"abundance", "density", "presence", "absence", "presence_absence", "predicted", "modelled_range",
             "distribution_model", "risk_score", "threat_score", "trend", "derived_status"}


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
    """One in-memory deployment with the biodiversity source pack installed and its licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = 1_790_000_000_000  # 2026-09-21
        self.runtime = SourcePackRuntime(self.conn, now=self.tick, sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = BiodiversityStore(self.conn, now=self.tick)

    def tick(self) -> int:
        self.clock += 1_000
        return self.clock

    def advance(self, days: float) -> None:
        self.clock += int(days * 86_400_000)

    def adapters(self, later: bool = False) -> dict:
        if not later:
            return self.runtime.fixture_adapters(self.value["pack_id"], ROOT)
        installed = self.runtime._manifest(self.value["pack_id"])[0]
        overrides = {**LATER["gbif"], **LATER["iucn"]}
        result = {}
        for source in installed["sources"]:
            pages = copy.deepcopy(json.loads((ROOT / source["fixture"]["path"]).read_text())["native_pages"])
            known = {p["request"] for p in pages}
            for page in pages:
                if page["request"] in overrides:
                    page.update(copy.deepcopy(overrides[page["request"]]))
            pages += [{"request": k, "status": 200, **copy.deepcopy(v)} for k, v in overrides.items()
                      if k not in known and source["biodiversity"]["provider"] == "iucn" and "/api/v4/" in k]
            result[source["source_id"]] = self.runtime.factory.compile(
                source, transport=fixture_transport(pages), secret=FIXTURE_SECRET)
        return result

    def run(self, key: str = "biodiversity-1", *, source_ids: list[str] | None = None, later: bool = False) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "biodiversity",
                   "max_results": 1000, "max_bytes": 20_000_000, "timeout_ms": 60_000,
                   "source_ids": source_ids or SOURCES}
        return self.runtime.run(request, principal_id="operator", adapters=self.adapters(later),
                                dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self

    def place(self, name: str, geometry: dict, *, source_ids: dict | None = None) -> dict:
        from src.kb.geospatial import GeospatialStore

        return GeospatialStore(self.conn).register_place(
            NS, name, "district", names=[{"value": name, "language": "de", "kind": "canonical"}],
            source_ids=source_ids or {"fixture": name.casefold()}, parent_ids=[], principal_id="alice",
            scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"}, geometry=geometry)

    def seed_papers(self) -> dict[str, str]:
        """One fictional Science literature record the GBIF bird dataset cites by DOI."""
        from services.ingest.common.document_model import Document
        from src.ingestion.document_store import DocumentStore

        document = Document(document_id="bio-doc:urban-sparrows", source_type="paper", source_id="crossref",
                            language="en", ingested_at=self.tick(), url="https://example.org/urban-sparrows",
                            title="Urban sparrows of a fictional city", content="Urban sparrows (fictional)",
                            authors=["A. Fictional"],
                            metadata={"content_representation": "plain-text-abstract",
                                      "doi": "10.5555/fict.bio.2099.1"})
        outcome = DocumentStore(self.conn).upsert([document.to_dict()])
        assert not outcome.invalid, outcome.dead_letter
        return {"urban-sparrows": document.document_id}
