"""Offline harness for the Legal federal-statutes feature (#2105): fictional statute fixtures through the real adapters.

Everything under ``tests/fixtures/federal_statutes`` is authored in the
documented provider shapes and names a fictional statute (MPHG), fictional
acts (BGBl. 2030 I Nr. 45/46), fictional decisions and a fictional EU
directive; nothing here is live coverage. Pages go through
:class:`GiiStatuteAdapter`, :class:`RisStatuteAdapter`, :class:`BgblActAdapter`
and :class:`RiiDecisionAdapter` with the fixture transport, then through
:class:`LegalStore` exactly as the runtime projector calls it.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from src.ingestion.legal_sources import (
    BgblActAdapter,
    GiiStatuteAdapter,
    RiiDecisionAdapter,
    RisStatuteAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.legal import LegalStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/federal_statutes"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
SCOPES = {
    "knowledge:legal:read",
    "knowledge:legal:write",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:political:dossier:read",
}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
MPHG = {
    "jurabk": "MPHG",
    "gii_path": "mphg",
    "names": ["Musterpapierhandelsgesetz", "Musterpapierhandelsgesetzes"],
}
SOURCE_IDS = {
    "gii": "gii-federal-statutes",
    "ris": "ris-federal-statute-versions",
    "bgbl": "bgbl-federal-promulgations",
    "rii": "rii-federal-decisions",
}
RIS_BASE = "/v1/legislation/eli/bund/bgbl-1/2029/101"
DOSSIER_NS = "research"  # the lobbying harness's dossier namespace


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def source(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(
        next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key])
    )


def fictional(key: str) -> dict:
    """The production source with its selection replaced by the fictional MPHG statute (or fictional decisions)."""
    item = source(key)
    selection = item["legal"]["selection"]
    if key == "gii":
        selection["statutes"] = [{"jurabk": "MPHG", "gii_path": "mphg"}]
    elif key == "ris":
        selection.update({"statutes": [{"jurabk": "MPHG"}], "max_expressions": 5})
    elif key == "bgbl":
        selection["statutes"] = [MPHG]
    else:
        item["legal"]["selection"] = {"decisions": ["KORE900012031", "KORE900022031"]}
    return item


def zipped(name: str) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("BJNR999010029.xml", date_time=(2030, 1, 1, 0, 0, 0))
        archive.writestr(info, (FIXTURES / name).read_bytes())
    return base64.b64encode(buffer.getvalue()).decode()


def gii_pages(snapshot: str) -> list[dict]:
    return [
        {
            "request": "/gii-toc.xml",
            "status": 200,
            "body": (FIXTURES / "gii_toc.xml").read_text(),
        },
        {
            "request": "/mphg/xml.zip",
            "status": 200,
            "body_encoding": "base64",
            "body": zipped(f"gii_mphg_{snapshot}.xml"),
        },
    ]


def ris_pages(
    expressions: tuple[str, ...] = ("2029-06-01", "2030-04-01"),
) -> list[dict]:
    search = json.loads((FIXTURES / "ris_search_mphg.json").read_text())
    search["member"] = [
        m
        for m in search["member"]
        if m["item"]["abbreviation"] != "MPHG"
        or any(
            e in m["item"]["workExample"]["legislationIdentifier"] for e in expressions
        )
    ]
    pages = [{"request": "/v1/legislation", "status": 200, "body": search}]
    for day in expressions:
        pages.append(
            {
                "request": f"{RIS_BASE}/{day}/1/deu/regelungstext-1.xml",
                "status": 200,
                "body": (FIXTURES / f"ris_mphg_{day}.xml").read_text(),
            }
        )
    return pages


def bgbl_pages() -> list[dict]:
    pages = [
        {
            "request": "/rss/bgbl-1.xml",
            "status": 200,
            "body": (FIXTURES / "bgbl_listing.xml").read_text(),
        }
    ]
    for number in (45, 46):
        pages.append(
            {
                "request": f"/eli/bund/bgbl-1/2030/{number}/regelungstext-verkuendung-1.xml",
                "status": 200,
                "body": (FIXTURES / f"bgbl_2030_{number}.xml").read_text(),
            }
        )
    return pages


def rii_pages() -> list[dict]:
    pages = []
    for doknr in ("KORE900012031", "KORE900022031"):
        pages.append(
            {
                "request": f"/jportal/docs/bsjrs/jb-{doknr}.zip",
                "status": 200,
                "body": (FIXTURES / f"rii_{doknr}.xml").read_text(),
            }
        )
    return pages


ADAPTERS = {
    "gii": GiiStatuteAdapter,
    "ris": RisStatuteAdapter,
    "bgbl": BgblActAdapter,
    "rii": RiiDecisionAdapter,
}


def drain(key: str, pages: list[dict], item: dict | None = None):
    adapter = ADAPTERS[key](item or fictional(key), transport=fixture_transport(pages))
    records, receipts, cursor = [], [], None
    for _ in range(int(adapter.definition["limits"]["max_pages"])):
        page = adapter.fetch_page(
            {"operation": "records", "parameters": {}, "limit": 100}, cursor=cursor
        )
        records.extend(page.records)
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, receipts


def apply(
    conn, key: str, pages: list[dict], *, at: str, run_id: str | None = None
) -> dict:
    records, receipts = drain(key, pages)
    store = LegalStore(conn)
    for receipt in receipts:
        store.record_outcome(NS, run_id or f"{key}@{at}", SOURCE_IDS[key], receipt)
    return store.project(
        NS,
        records,
        run_id=run_id or f"{key}@{at}",
        source_id=SOURCE_IDS[key],
        observed_at_ms=ms(at),
    )


def cellar_directive(conn) -> dict:
    """A fictional CELLAR record of Directive (EU) 2030/77 so the act's implementation statement can resolve."""
    record = {
        "contract": "noesis-native-regional-v1",
        "provider": "cellar",
        "provider_id": "cellar:fiktiv-2030-77",
        "kind": "legislation",
        "language": "de",
        "title": "Richtlinie (EU) 2030/77 (fiktiv)",
        "fields": {
            "work": "http://publications.europa.eu/resource/cellar/fiktiv-2030-77",
            "celex": "32030L0077",
            "eli_identifiers": ["http://data.europa.eu/eli/dir/2030/77/oj"],
        },
        "native": {"xml_sha256": "0" * 64},
        "sections": [],
    }
    return LegalStore(conn).project(
        NS,
        [record],
        run_id="cellar-fiktiv",
        source_id="cellar-fixture",
        observed_at_ms=ms("2030-01-10"),
    )


def dip_dossier(conn, *, quote: str = "Verkündung: BGBl I 2030 Nr. 45") -> dict:
    """A fictional Bundestag DIP dossier whose adoption stage states the act's BGBl citation."""
    from src.domains.political.legislative_dossiers import LegislativeDossierStore
    from tests.unit import lobbying_harness as lh

    store, de, _eu = lh._documents(conn)
    document = lh._add(
        store,
        de,
        source_id="de-bundestag-dip",
        identity="21/9045",
        document_type="proposal",
        title="Entwurf eines Gesetzes zur Änderung des Musterpapierhandelsgesetzes (fiktiv)",
        content=f"Gesetzentwurf der Bundesregierung (fiktiv). {quote}.",
        political={
            "procedure_id": "proposal:de:mphg-aenderung-2030",
            "fixture": True,
            "legal_events": [
                {
                    "kind": "adoption",
                    "date": "2030-03-12",
                    "source_quote": quote,
                    "jurisdiction": "DE",
                }
            ],
        },
        observed_at=2000,
    )
    scopes = lh.DOSSIER_SCOPES | {f"document:{document}:read"}
    return LegislativeDossierStore(conn, now=lambda: 3000).save(
        lh.DOSSIER_NS,
        "mphg-2030",
        "DE",
        "proposal:de:mphg-aenderung-2030",
        [lh._ref(conn, document)],
        principal_id="alice",
        scopes=scopes,
    )


def connection():
    return duckdb.connect(":memory:")


def load_all(conn) -> None:
    """The full fictional journey's acquisitions in date order."""
    cellar_directive(conn)
    apply(conn, "gii", gii_pages("2030-03-01"), at="2030-03-01")
    apply(conn, "ris", ris_pages(), at="2030-04-05")
    apply(conn, "bgbl", bgbl_pages(), at="2030-03-15")
    apply(conn, "gii", gii_pages("2030-06-01"), at="2030-06-01")
    apply(conn, "rii", rii_pages(), at="2031-06-01")
