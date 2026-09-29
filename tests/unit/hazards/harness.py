"""Offline harness for the Natural Hazards pack (#2207): authored documents replayed through the real adapter.

Every file under ``tests/fixtures/hazards`` is authored in the provider's
documented shape (GloFAS: a declared export shape) and names fictional events,
storms, fires and values dated 2099; nothing here is live coverage. Documents go
through :class:`~src.ingestion.hazard_sources.HazardAdapter` (the connector the
source-pack runtime compiles) and :class:`~src.kb.hazards_store.HazardProjector`.

``python -m tests.unit.hazards.harness`` rebuilds the source-pack fixtures
(``tests/fixtures/source_packs/hazards-*.json``) from the latest documents and
re-pins their hashes in ``config/source_packs/natural-hazards.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.hazard_sources import HazardAdapter, fixture_request, fixture_transport, replay_native_fixture
from src.ingestion.source_packs import validate_source_pack
from src.kb import hazards_records as hr
from src.kb.hazards_store import HazardProjector

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/hazards"
PACK = ROOT / "config/source_packs/natural-hazards.json"
NS = "hazards"
SCOPES = {hr.READ_SCOPE, hr.WRITE_SCOPE, f"namespace:{NS}:read", f"namespace:{NS}:write",
          "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
          "knowledge:subscriptions:read", "knowledge:subscriptions:write"}
REVIEW_SCOPES = SCOPES | {hr.REVIEW_SCOPE}
SOURCES = {"usgs": "usgs-earthquakes", "emsc": "emsc-earthquakes", "gdacs": "gdacs-events", "nhc": "nhc-advisories",
           "effis": "effis-burnt-areas", "glofas": "glofas-notifications"}
# The latest state of every declared document (what the source-pack fixtures pin).
LATEST = {
    "usgs": ["usgs_query_2099-08-11.geojson", "usgs_detail_us7000zz01_2099-08-11.geojson",
             "usgs_detail_us7000zz01_2099-08-11.geojson"],
    "emsc": ["emsc_query_2099-08-11.json"],
    "gdacs": ["gdacs_events_2099-09-02.geojson"],
    "nhc": ["nhc_al052099_fstadv_011.txt", "nhc_al052099_fstadv_012.txt", "nhc_al052099_public_012a.txt",
            "nhc_al052099_fstadv_013.txt"],
    "effis": ["effis_burnt_areas_2099-08-15.geojson"],
    "glofas": ["glofas_notifications_2099-08-20.json"],
}
# Earlier states, acquired first in the offline journeys (document index, file).
EARLIER = {
    "usgs": [(0, "usgs_query_2099-08-10T0330.geojson"), (1, "usgs_detail_us7000zz01_2099-08-10T0345.geojson")],
    "emsc": [(0, "emsc_query_2099-08-10T0330.json")],
    "gdacs": [(0, "gdacs_events_2099-08-10T0400.geojson")],
    "nhc": [(0, "nhc_al052099_fstadv_011.txt"), (1, "nhc_al052099_fstadv_012.txt")],
    "effis": [(0, "effis_burnt_areas_2099-08-13.geojson")],
}
SCENARIOS = {
    "usgs": ["authored-fixture", "reviewed-revision", "deleted-event", "merged-secondary-id", "pager-version"],
    "emsc": ["authored-fixture", "author-agency-revision"],
    "gdacs": ["authored-fixture", "episode-revision", "glide", "multi-hazard"],
    "nhc": ["authored-fixture", "forecast-advisory", "intermediate-advisory", "watches-warnings", "cone-locator"],
    "effis": ["authored-fixture", "burnt-area-polygon"],
    "glofas": ["authored-fixture", "key-gated", "modelled-output"],
}


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(provider: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[provider]))


def body(name: str) -> str:
    return (FIXTURES / name).read_text()


def _content_type(name: str) -> str:
    return "text/plain" if name.endswith(".txt") else "application/json"


def records(provider: str, files) -> list[dict]:
    """Every record of one run over ``files`` = [(document index, filename)] (only those documents are fetched)."""

    item = source(provider)
    documents = item["natural_hazards"]["documents"]
    item["natural_hazards"]["documents"] = [documents[index] for index, _ in files]
    natives = [{"request": fixture_request(documents[index]["url"]), "status": 200,
                "headers": {"Content-Type": _content_type(name)}, "body": body(name)} for index, name in files]
    secret = "fixture-token" if item["auth"]["kind"] == "required-secret" else None
    adapter = HazardAdapter(item, transport=fixture_transport(natives), secret=secret)
    pages, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "observe", "parameters": {}, "limit": 1000}, cursor=cursor)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return item, pages


def apply(conn, provider: str, files, *, at: str, run_id: str | None = None) -> dict:
    """Acquire ``files`` at ``at`` (the acquisition clock) and project every page."""

    item, pages = records(provider, files)
    projector = HazardProjector(conn)
    projector.store.now = lambda: ms(at)
    totals = {"revisions": 0, "unchanged": 0, "late": 0, "places": 0}
    for number, page in enumerate(pages):
        out = projector.project_page(run_id=run_id or f"{provider}:{at}:{number}", manifest=manifest(), source=item,
                                     records=page.records, documents=[], page_receipt=dict(page.receipt),
                                     principal_id="operator")
        for key in totals:
            totals[key] += out[key]
    projector.finish_source(run_id=run_id or f"{provider}:{at}", manifest=manifest(), source=item, status="complete",
                            principal_id="operator")
    return totals


def latest(provider: str):
    return list(enumerate(LATEST[provider]))


def load_all(conn) -> None:
    """The offline journey's acquisitions: earlier states first, then the latest state of every source."""

    apply(conn, "usgs", EARLIER["usgs"], at="2099-08-10T03:50:00Z")
    apply(conn, "emsc", EARLIER["emsc"], at="2099-08-10T03:50:00Z")
    apply(conn, "gdacs", EARLIER["gdacs"], at="2099-08-10T04:10:00Z")
    apply(conn, "effis", EARLIER["effis"], at="2099-08-13T12:00:00Z")
    apply(conn, "usgs", latest("usgs"), at="2099-08-11T00:00:00Z")
    apply(conn, "emsc", latest("emsc"), at="2099-08-11T00:00:00Z")
    apply(conn, "effis", latest("effis"), at="2099-08-15T12:00:00Z")
    apply(conn, "glofas", latest("glofas"), at="2099-08-20T06:00:00Z")
    apply(conn, "nhc", EARLIER["nhc"], at="2099-09-01T09:30:00Z")
    apply(conn, "nhc", latest("nhc"), at="2099-09-01T15:30:00Z")
    apply(conn, "gdacs", latest("gdacs"), at="2099-09-02T00:00:00Z")


def fixture_document(provider: str) -> dict:
    item = source(provider)
    pages, seen = [], set()
    for document, name in zip(item["natural_hazards"]["documents"], LATEST[provider], strict=True):
        request = fixture_request(document["url"])
        if request in seen:
            continue
        seen.add(request)
        pages.append({"request": request, "status": 200, "headers": {"Content-Type": _content_type(name)},
                      "body": body(name)})
    return {"captured": None, "native_pages": pages, "provider": "authored",
            "note": "Authored documents in the provider's documented shape (GloFAS: a declared export shape); every "
                    "event, storm, fire, identifier and value is fictional and dated 2099 (not provider data).",
            "scenarios": SCENARIOS[provider]}


def build(write: bool = True) -> dict:
    """Rebuild the source-pack fixtures and their pins; returns {source_id: (sha256, expected_output_hash)}."""

    raw = json.loads(PACK.read_text())
    pins = {}
    for provider, source_id in SOURCES.items():
        fixture = fixture_document(provider)
        text = json.dumps(fixture, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        path = ROOT / f"tests/fixtures/source_packs/hazards-{provider}.json"
        if write:
            path.write_text(text)
        output = replay_native_fixture(source(provider), fixture)
        output_hash = hashlib.sha256(json.dumps(output, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False).encode()).hexdigest()
        pins[source_id] = (hashlib.sha256(text.encode()).hexdigest(), output_hash)
    for item in raw["sources"]:
        item["fixture"]["sha256"], item["fixture"]["expected_output_hash"] = pins[item["source_id"]]
    if write:
        PACK.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return pins


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
