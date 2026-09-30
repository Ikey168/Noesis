"""Offline harness for books, music and authority metadata (#2225): synthetic fixtures through the real runtime.

Installs the scientific source pack (``primary-scientific-evidence``) with
licences accepted and runs the seven ``media-metadata`` sources (operation
``media``) through their pinned fixtures. Nothing here is live coverage.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.media_metadata import MediaMetadataStore

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/scientific.json"
NS = "global"
READ = {"knowledge:cultural:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:cultural:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:cultural:review"}
ALL = REVIEW | {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
MEDIA_SOURCES = ["openlibrary-media", "musicbrainz-media", "wikidata-media", "dnb-gnd-media", "dnb-catalogue-media",
                 "loc-lcnaf-media", "loc-catalogue-media"]
# Fixture identities (all synthetic).
WORK_OLID, OTHER_WORK_OLID, AUTHOR_OLID, MERGED_AUTHOR_OLID = "OL9990001W", "OL9990002W", "OL9990001A", "OL9990009A"
EDITION_OLID, SECOND_EDITION_OLID, CLAIMING_EDITION_OLID = "OL9990001M", "OL9990002M", "OL9990003M"
ISBN, SECOND_ISBN, UNKNOWN_ISBN = "9783000000010", "9783000000027", "9791000000015"
AUTHOR_QID, WORK_QID, EDITION_QID, BAND_QID, SONG_QID, REDIRECTED_QID = (
    "Q999900001", "Q999900002", "Q999900003", "Q999900010", "Q999900011", "Q999900019")
GND, OLD_GND, LCNAF, LCCN, IDN = "1099990001", "1099990002", "n2099000001", "2019001234", "123456789X"
ARTIST_MBID = "a1000000-0000-4000-8000-000000000001"
OLD_RECORDING_MBID, RECORDING_MBID = "b1000000-0000-4000-8000-000000000001", "b1000000-0000-4000-8000-000000000002"
WORK_MBID, RELEASE_MBID = "c1000000-0000-4000-8000-000000000001", "d1000000-0000-4000-8000-000000000001"
UNKNOWN_MBID = "e1000000-0000-4000-8000-000000000001"
ISRC = "ZZFIX2600001"


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str, value: dict | None = None) -> dict:
    value = value or manifest()
    return copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == source_id))


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())["native_pages"])


class Env:
    """One in-memory deployment with the scientific pack installed and licences accepted."""

    def __init__(self, conn: Any | None = None) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = iter(range(1_000, 10_000_000_000, 1_000))
        self.runtime = SourcePackRuntime(self.conn, now=lambda: next(self.clock), sleep=lambda _d: None)
        for item in self.value["sources"]:
            self.runtime.accept_license(self.value["pack_id"], item["source_id"], principal_id="operator")
        self.store = MediaMetadataStore(self.conn, now=lambda: next(self.clock))

    def run(self, key: str = "media-1", *, source_ids: list[str] | None = None, adapters=None) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "media", "max_results": 100,
                   "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": source_ids or MEDIA_SOURCES}
        return self.runtime.run(request, principal_id="operator",
                                adapters=adapters or self.runtime.fixture_adapters(self.value["pack_id"], ROOT),
                                dns_resolver=lambda _h: ["8.8.8.8"], secret_resolver=lambda _ref: None)

    def record_id(self, source_name: str, native_id: str) -> str:
        return self.store.resolve_record(NS, f"{source_name}:{native_id}")
