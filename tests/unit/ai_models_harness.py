"""Shared offline harness for the Technology AI models and datasets tests (#2742): pinned fixtures, real adapter."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.ai_models_sources import (
    FIXTURE_SECRET,
    AiModelsAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.ai_models_store import AiModelsProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/ai_models"
PACK = ROOT / "config/source_packs/technology-ai-models.json"
NS = "global"
SCOPES = {
    "knowledge:technical:ai-models:read",
    "knowledge:technical:ai-models:write",
    "knowledge:technical:ai-models:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:oss:read",
    "knowledge:science:research-entities:read",
    "knowledge:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:technical:ai-models:read", "namespace:global:read"}
SOURCES = {"hub": "huggingface-hub", "openml": "openml", "epoch": "epoch-ai"}
REVISIONS = {"hub": "hub_revision.json", "openml": "openml_revision.json", "epoch": "epoch_revision.json"}
SHAS = json.loads((FIXTURES / "shas.json").read_text())
MODEL = "example-org/fixture-model"
SMALL = "example-org/fixture-small-model"
CORPUS = "example-org/fixture-corpus"
# Retrieval clocks (epoch ms): every fixture revision (2094-2097) precedes the retrieval that reads it.
FIRST_RETRIEVAL = 4_007_836_800_000  # 2097-01-01
SECOND_RETRIEVAL = 4_039_372_800_000  # 2098-01-01


def day_ms(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


assert day_ms("2097-01-01") == FIRST_RETRIEVAL and day_ms("2098-01-01") == SECOND_RETRIEVAL


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(name: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def adapter(name: str, *, revision: bool = False, transport=None, item=None, secret: str | None = FIXTURE_SECRET,
            **kwargs) -> AiModelsAdapter:
    return AiModelsAdapter(item or source(name), transport=transport or fixture_transport(pages(name, revision)),
                           secret=secret, **kwargs)


def fetch(name: str, *, revision: bool = False, transport=None, item=None, secret: str | None = FIXTURE_SECRET,
          **kwargs) -> list[list[dict[str, Any]]]:
    item = item or source(name)
    reader = adapter(name, revision=revision, transport=transport, item=item, secret=secret, **kwargs)
    out, cursor = [], None
    while True:
        page = reader.fetch_page({"operation": "registry", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                 cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def statements(name: str, *, revision: bool = False, **kwargs) -> list[dict[str, Any]]:
    return [r["ai_statement"] for page in fetch(name, revision=revision, **kwargs) for r in page]


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one unit per page), as the runtime would."""
    item = source(name)
    projector = AiModelsProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision, item=item):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in SOURCES:
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


SPDX_LICENCES = [
    {"id": "Apache-2.0", "name": "Apache License 2.0"},
    {"id": "MIT", "name": "MIT License"},
    {"id": "CC-BY-4.0", "name": "Creative Commons Attribution 4.0 International"},
]


def load_spdx(conn, namespace: str = NS) -> None:
    """A small pinned SPDX License List release (fictional version 3.99) in the OSS ecosystems store."""
    from src.kb.oss_ecosystem_store import OssEcosystemStore

    OssEcosystemStore(conn).apply(namespace, [{"record_type": "spdx_list_release", "source": "spdx",
                                               "list_version": "3.99", "licences": SPDX_LICENCES, "exceptions": []}],
                                  run_id="run:spdx", scopes={"knowledge:oss:write", f"namespace:{namespace}:write"},
                                  observed_at_ms=FIRST_RETRIEVAL)


def record(conn, kind: str, native: str) -> dict[str, Any]:
    from src.kb.ai_models_store import AiModelsStore

    found = AiModelsStore(conn, initialize=False).find(NS, kind, native)
    assert found is not None, (kind, native)
    return found
