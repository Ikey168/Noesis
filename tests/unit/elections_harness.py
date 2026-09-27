"""Offline harness for the Political elections feature (#1908): authored result files replayed through the real adapter.

Every file under ``tests/fixtures/elections`` is authored in the provider's
documented shape and names fictional areas, parties and candidates only;
nothing here is live coverage. Result files go through
:class:`ElectionResultsAdapter` (the connector the runtime compiles) and
:class:`ElectionProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.election_sources import ElectionResultsAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.elections import ElectionProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/elections"
PACK = ROOT / "config/source_packs/political.json"
NS = "global"
READ = "knowledge:political:elections:read"
WRITE = "knowledge:political:elections:write"
REVIEW = "knowledge:political:elections:review"
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {REVIEW, "knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
SOURCES = {
    "de-btw": "de-bundeswahlleiterin-btw-results",
    "de-be": "de-berlin-agh-results",
    "gb": "gb-electoral-commission-ge-results",
    "us": "us-mit-election-lab-countypres",
}
DE_PRELIMINARY = "de_btw_kerg_2099_vorlaeufig.csv"
DE_FINAL = "de_btw_kerg_2099_endgueltig.csv"
BERLIN = "de_be_agh_2099_vorlaeufig.csv"
UK = "gb_ec_ge_2099.csv"
US = "us_medsl_countypres_2099.csv"
DE_ELECTION = "de-bt:2099-03-01"
UK_ELECTION = "gb-general:2099-05-07"
UK_LAST_MODIFIED = "Fri, 15 May 2099 10:00:00 GMT"


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(key: str) -> dict:
    item = copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[key])
    )
    if key == "gb":
        # The fictional fixture belongs to a fictional election, not to the manifest's live one.
        item["elections"]["election"] = {
            "kind": "general",
            "date": "2099-05-07",
            "native_id": "ge-2099",
        }
    if key == "us":
        item["elections"]["election_dates"] = {"2099": "2099-11-03"}
    return item


def page(
    key: str,
    filename: str,
    *,
    body: str | None = None,
    item: dict | None = None,
    headers: dict | None = None,
    final_url: str | None = None,
    limit: int = 1000,
):
    item = item or source(key)
    parts = urlsplit(item["endpoint"])
    request = parts.path + ("?" + parts.query if parts.query else "")
    if headers is None:
        headers = {"Content-Type": "text/csv"}
        if key == "gb":
            headers["Last-Modified"] = UK_LAST_MODIFIED
    native = {
        "request": request,
        "status": 200,
        "headers": headers,
        "body": body if body is not None else (FIXTURES / filename).read_text(),
        **({"final_url": final_url} if final_url else {}),
    }
    adapter = ElectionResultsAdapter(item, transport=fixture_transport([native]))
    return adapter.fetch_page(
        {"operation": "release", "parameters": {}, "limit": limit}, cursor=None
    ), item


def apply(
    conn,
    key: str,
    filename: str,
    *,
    run_id: str | None = None,
    body: str | None = None,
    item: dict | None = None,
    headers: dict | None = None,
) -> dict:
    fetched, item = page(key, filename, body=body, item=item, headers=headers)
    return ElectionProjector(conn).project_page(
        run_id=run_id or f"run:{filename}",
        manifest=None,
        source=item,
        records=fetched.records,
        documents=[],
        page_receipt=fetched.receipt,
        principal_id="operator",
    )[0]


def load_results(conn) -> None:
    apply(conn, "de-btw", DE_PRELIMINARY)
    apply(conn, "de-btw", DE_FINAL)
    apply(conn, "de-be", BERLIN)
    apply(conn, "gb", UK)
    apply(conn, "us", US)


def contest_id(conn, election_id: str, scheme: str, native_id: str, ballot: str) -> str:
    return conn.execute(
        "SELECT contest_id FROM election_contests WHERE election_id=? AND unit_scheme=? AND unit_native_id=? "
        "AND ballot=?",
        [election_id, scheme, native_id, ballot],
    ).fetchone()[0]


class Clock:
    def __init__(self, start: int = 4_080_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
