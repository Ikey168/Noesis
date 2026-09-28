"""Offline harness for the Economics demographics feature (#1914): authored responses replayed through the real adapter.

Every file under ``tests/fixtures/demographics`` is authored in the provider's
documented shape and states fictional values only (origins, operations and
admin areas are fictional too); nothing here is live coverage. Responses go
through :class:`DemographicsAdapter` (the connector the runtime compiles) and
:class:`DemographicProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.demographic_sources import (
    FIXTURE_SECRET,
    DemographicsAdapter,
    fixture_request,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.demographics import DemographicProjector, DemographicStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/demographics"
PACK = ROOT / "config/source_packs/economic.json"
NS = "global"
READ = "knowledge:demographics:read"
WRITE = "knowledge:demographics:write"
REVIEW = "knowledge:demographics:review"
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:geospatial:read",
    "knowledge:legal:read",
    "knowledge:political:dossier:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {REVIEW}
READ_ONLY = {READ, f"namespace:{NS}:read"}
SOURCES = {
    "eurostat": "eurostat-demography-migration",
    "unhcr": "unhcr-refugee-data-finder",
    "dtm": "iom-dtm-idps",
    "destatis": "destatis-genesis-population-migration",
    "berlin": "statistik-bb-bezirke",
}
# Per source: every document's response files, in document order (GENESIS: metadata, data).
FILES = {
    "eurostat": [
        "eurostat_demo_pjan_de_2099-03.json",
        "eurostat_demo_r_pjanaggr3_de3_2099-03.json",
        "eurostat_migr_imm1ctz_de_2099-03.json",
        "eurostat_migr_asyappctza_de_2099-03.json",
        "eurostat_migr_asydcfsta_de_2099-03.json",
    ],
    "unhcr": ["unhcr_population_2097_2098.json"],
    "dtm": ["dtm_admin1_fictional_operation.json"],
    "destatis": [
        ("genesis_12411-0010_metadata.json", "genesis_12411-0010.csv"),
        ("genesis_12711-0005_metadata.json", "genesis_12711-0005.csv"),
    ],
    "berlin": ["statbb_bezirke_2098.csv", "statbb_bezirke_2099.csv"],
}
PJAN_SEPTEMBER = "eurostat_demo_pjan_de_2099-09.json"
BAMF_SHEET = "bamf_aktuelle_zahlen_2098-12.json"
# Fictional publisher references an operator would declare from the dataset metadata (#1988).
EUROSTAT_REFERENCES = [
    {
        "scheme": "celex",
        "identifier": "32099R0999",
        "relation": "reported_under",
        "locator": "ESMS 3.1",
    }
]
BERLIN_REFERENCES = [
    {
        "scheme": "de-drucksache",
        "identifier": "99/2001",
        "relation": "referenced_in",
        "locator": "Vorbemerkung",
    }
]


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(key: str, *, references: bool = False) -> dict:
    item = copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[key])
    )
    if references and key == "eurostat":
        item["demographics"]["documents"][2]["references"] = EUROSTAT_REFERENCES
    if references and key == "berlin":
        item["demographics"]["documents"][0]["references"] = BERLIN_REFERENCES
    return item


def body(filename: str) -> str:
    return (FIXTURES / filename).read_text()


def natives(key: str, item: dict, pairs, *, headers=None, final_url=None) -> list[dict]:
    pages = []
    for index, files in pairs:
        document = item["demographics"]["documents"][index]
        parts = (
            [("metadata", files[0]), ("data", files[1])]
            if isinstance(files, tuple)
            else [("data", files)]
        )
        for part, name in parts:
            text = (
                body(name) if len(name) < 200 and (FIXTURES / name).exists() else name
            )
            pages.append(
                {
                    "request": fixture_request(document, item["endpoint"], part=part),
                    "status": 200,
                    "headers": headers or {},
                    "body": text,
                    **({"final_url": final_url} if final_url else {}),
                }
            )
    return pages


def fetch(
    key: str,
    index: int,
    files=None,
    *,
    item=None,
    headers=None,
    final_url=None,
    secret=FIXTURE_SECRET,
):
    item = item or source(key)
    files = files if files is not None else FILES[key][index]
    adapter = DemographicsAdapter(
        item,
        transport=fixture_transport(
            natives(key, item, [(index, files)], headers=headers, final_url=final_url)
        ),
        secret=secret,
    )
    page = adapter.fetch_page(
        {"operation": "release", "parameters": {}, "limit": 1000},
        cursor=None if index == 0 else str(index),
    )
    return page, item


def apply(
    conn, key: str, index: int, files=None, *, item=None, now=None, run_id=None
) -> dict:
    page, item = fetch(key, index, files, item=item)
    projector = DemographicProjector(conn)
    if now is not None:
        projector.store.now = now
    return projector.project_page(
        run_id=run_id or f"run:{key}:{index}",
        manifest=None,
        source=item,
        records=page.records,
        documents=[],
        page_receipt=page.receipt,
        principal_id="operator",
    )[0]


def load_all(conn, *, references: bool = False, now=None) -> list[dict]:
    results = []
    for key, files in FILES.items():
        item = source(key, references=references)
        for index in range(len(files)):
            results.append(apply(conn, key, index, item=item, now=now))
    return results


def load_bamf(conn, *, principal_id: str = "operator-anna") -> dict:
    return DemographicStore(conn).import_sheet(
        NS, json.loads(body(BAMF_SHEET)), principal_id=principal_id, scopes=SCOPES
    )


def series_id(conn, **filters) -> str:
    (found,) = DemographicStore(conn, initialize=False).find_series(NS, **filters)
    return found["series_id"]


class Clock:
    def __init__(self, start: int = 4_100_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
