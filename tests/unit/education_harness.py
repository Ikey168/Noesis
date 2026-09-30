"""Shared offline harness for the Science education-statistics tests (#2227): pinned fixtures through the real
adapter and projector."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.education_sources import EducationStatisticsAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.education_statistics import EducationProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/education"
NS = "global"
SCOPES = {
    "knowledge:education:read",
    "knowledge:education:write",
    "knowledge:education:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:funding:read",
    "knowledge:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:education:read", "namespace:global:read"}
SOURCES = {
    "ipeds": "ipeds-institution-statistics",
    "eter": "eter-institution-statistics",
    "uis": "unesco-uis-education-indicators",
    "oecd": "oecd-eag-education-indicators",
    "eurostat": "eurostat-rd-statistics",
}
REVISIONS = {
    "ipeds": "ipeds_final_2099-10.json",
    "uis": "uis_release_2099-09.json",
    "eurostat": "eurostat_update_2099-04.json",
}
FIRST_RETRIEVAL = 4_102_444_800_000  # 2100-01-01
SECOND_RETRIEVAL = 4_105_123_200_000  # 2100-02-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/scientific.json").read_text()))


def source(name: str, *, revision: bool = False) -> dict[str, Any]:
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name]))
    if revision:
        replaced = revision_fixture(name)["documents"]
        documents = item["education_statistics"]["documents"]
        for index, document in enumerate(documents):
            if document["label"] in replaced:
                documents[index] = replaced[document["label"]]
        requests = {p["request"] for p in revision_fixture(name)["native_pages"]}
        from src.ingestion.education_sources import fixture_request

        fmt = item["education_statistics"]["format"]
        item["education_statistics"]["documents"] = [
            d for d in documents if fixture_request(fmt, d, item["endpoint"]) in requests
        ]
    return item


def revision_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / REVISIONS[name]).read_text())


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return revision_fixture(name)["native_pages"]
    item = source(name)
    return json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    """Every page's records for the source's declared documents."""
    item = source(name, revision=revision)
    adapter = EducationStatisticsAdapter(item, transport=transport or fixture_transport(pages(name, revision)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]}, cursor=cursor
        )
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source into the store (one release per page), as the runtime would."""
    item = source(name, revision=revision)
    projector = EducationProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in REVISIONS:
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def ror_records(*, later: bool = False) -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "ror_records.json").read_text())
    return data["later"] if later else data["records"]


def day_ms(day: str) -> int:
    from datetime import date, datetime, timezone

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
