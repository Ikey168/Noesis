"""Offline harness for the Market ``insurance`` feature (#2230): fictional fixtures through the real adapter.

Everything under ``tests/fixtures/insurance`` is authored in the documented shapes (IN01) and names fictional
insurers and events; nothing here is live coverage. Pages go through :class:`InsuranceAdapter` with the fixture
transport and then through :class:`InsuranceProjector` exactly as the source-pack runtime calls them.
``python -m tests.unit.insurance_harness`` regenerates the SFCR PDF fixtures (PyMuPDF) and rewrites the pinned
production fixtures and their hashes in ``config/source_packs/market-insurance.json``.
"""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import duckdb

from src.domains.market.insurance import InsuranceProjector
from src.ingestion.insurance_sources import InsuranceAdapter, fixture_transport
from src.ingestion.source_packs import _digest, validate_source_pack

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/insurance"
PACK = ROOT / "config/source_packs/market-insurance.json"
NS = "market-insurance"
LEI_NS = "lei-global"
OWN_NS = "ownership"
HAZ_NS = "hazards"
GROUP = "Fiktiva Versicherung Gruppe SE"
GROUP_LEI = "529900FIKTIVAGRUP008"
SOLO = "Fiktiva Leben AG"
SOLO_LEI = "529900FIKTIVAVERS084"
PRINCIPAL = "analyst"
SCOPES = {
    "market:insurance:read",
    "market:insurance:write",
    "market:insurance:review",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:companies:read",
    f"namespace:{LEI_NS}:read",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
    f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write",
    "knowledge:hazards:read",
    "knowledge:hazards:write",
    f"namespace:{HAZ_NS}:read",
    f"namespace:{HAZ_NS}:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
READ_ONLY = {"market:insurance:read", f"namespace:{NS}:read"}
SOURCE_IDS = {
    "eiopa": "eiopa-insurance-statistics",
    "sfcr": "insurer-sfcr-allianz",
    "naic": "naic-market-share-reports",
    "florida": "florida-oir-catastrophe-claims",
    "ncei": "noaa-ncei-billion-dollar-disasters",
}
SFCR_TEXT = {
    "sfcr_group_2024.pdf": [
        GROUP,
        "Group Solvency and Financial Condition Report 2024",
        f"Legal Entity Identifier (LEI): {GROUP_LEI}",
        "S.23.01.22 Own funds (extract)",
        "R0680 Group SCR 5,432.1",
        "R0690 Ratio of eligible own funds to group SCR 212%",
        "S.25.01.22 Solvency Capital Requirement (extract)",
        "R0200 Solvency capital requirement excluding capital add-on 5,432.1",
    ],
    "sfcr_group_2024_corrected.pdf": [
        GROUP,
        "Group Solvency and Financial Condition Report 2024 (corrected version)",
        f"Legal Entity Identifier (LEI): {GROUP_LEI}",
        "S.23.01.22 Own funds (extract)",
        "R0680 Group SCR 5,432.1",
        "R0690 Ratio of eligible own funds to group SCR 210%",
        "S.25.01.22 Solvency Capital Requirement (extract)",
        "R0200 Solvency capital requirement excluding capital add-on 5,432.1",
    ],
    "sfcr_solo_2024.pdf": [
        SOLO,
        "Solvency and Financial Condition Report 2024",
        f"LEI: {SOLO_LEI}",
        "S.23.01.01 Own funds (extract)",
        "R0620 Ratio of Eligible own funds to SCR 180%",
        "S.25.01.21 Solvency Capital Requirement (extract)",
        "R0220 Solvency capital requirement 1,234.5",
    ],
}
# Simulated observation days per stage and the documents each stage acquires.
STAGES = {
    "eiopa": {"2025-07-01": ["eiopa_release_2025-06.csv"], "2026-01-05": ["eiopa_release_2025-12.csv"]},
    "sfcr": {"2025-05-02": ["sfcr_group_2024.pdf", "sfcr_solo_2024.pdf"], "2025-06-12": ["sfcr_group_2024_corrected.pdf"]},
    "naic": {"2025-06-01": ["naic_market_share_2024.pdf"]},
    "florida": {"2025-02-01": ["florida_oir_claims.csv"]},
    "ncei": {"2025-07-15": ["ncei_billion_dollar.csv"]},
}
DOCUMENTS = {
    "eiopa_release_2025-06.csv": {"release": "2025-06", "release_date": "2025-06-30"},
    "eiopa_release_2025-12.csv": {"release": "2025-12", "release_date": "2025-12-15"},
    "sfcr_group_2024.pdf": {"insurer": {"name": GROUP, "reporting_level": "group"}, "reporting_year": 2024,
                            "publication_date": "2025-04-30", "language": "en"},
    "sfcr_group_2024_corrected.pdf": {"insurer": {"name": GROUP, "reporting_level": "group"}, "reporting_year": 2024,
                                      "publication_date": "2025-06-10", "language": "en"},
    "sfcr_solo_2024.pdf": {"insurer": {"name": SOLO, "reporting_level": "solo"}, "reporting_year": 2024,
                           "publication_date": "2025-04-25", "language": "de"},
    "naic_market_share_2024.pdf": {"title": "Fictional market share report 2024", "period": "2024",
                                   "reference_id": "naic-fixture-2024", "publication_date": "2025-05-20"},
    "florida_oir_claims.csv": {},
    "ncei_billion_dollar.csv": {},
}


def ms(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------ authored binaries


def make_pdf(lines: list[str]) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    for number, line in enumerate(lines):
        page.insert_text((50, 60 + 18 * number), line, fontsize=10)
    return doc.tobytes(garbage=0, deflate=False, no_new_id=True)


def make_xlsx(csv_text: str, sheet: str = "Data") -> bytes:
    """A minimal XLSX (inline strings) holding the CSV's cells exactly as text."""
    rows = list(csv.reader(io.StringIO(csv_text)))

    def col(index: int) -> str:
        name = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    body = "".join(
        f'<row r="{r + 1}">'
        + "".join(
            f'<c r="{col(c)}{r + 1}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for c, value in enumerate(cells)
        )
        + "</row>"
        for r, cells in enumerate(rows)
    )
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        f'package/2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>",
        "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
        f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        f'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{main}"><sheetData>'
        f"{body}</sheetData></worksheet>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as book:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            book.writestr(info, text)
    return buffer.getvalue()


def body(name: str) -> bytes:
    if name == "naic_market_share_2024.pdf":
        return make_pdf(["Fictional market share report 2024", "Authored fixture; no NAIC content."])
    return (FIXTURES / name).read_bytes()


def write_pdf_fixtures() -> None:
    for name, lines in SFCR_TEXT.items():
        (FIXTURES / name).write_bytes(make_pdf(lines))


# ------------------------------------------------------------------ declarations


def production(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key]))


def rehash(source: dict) -> dict:
    source.pop("source_hash", None)
    source["source_hash"] = _digest(source)
    return source


def fictional(key: str, files: list[str]) -> dict:
    """The production source with the fictional scope and fixture documents on the endpoint host."""
    source = production(key)
    declared = source["insurance"]
    host = urlsplit(source["endpoint"]).hostname
    if key == "eiopa":
        declared["format"] = "eiopa-statistics-csv"
        declared.pop("sheet", None)
    if key == "sfcr":
        declared["insurers"] = [
            {"name": GROUP, "country": "DE", "reporting_level": "group"},
            {"name": SOLO, "country": "DE", "reporting_level": "solo"},
        ]
    if key == "florida":
        declared["events"] = ["Hurricane Fiktiva", "Hurricane Beispiel", "Hurricane Musterfall"]
    if key == "ncei":
        declared["events"] = ["Hurricane Fiktiva"]
    declared["documents"] = [
        {"label": name, "url": f"https://{host}/fixture/{name}", **DOCUMENTS.get(name, {})} for name in files
    ]
    return rehash(source)


def pages(files: list[str]) -> list[dict]:
    return [
        {"request": f"/fixture/{name}", "status": 200, "body_base64": base64.b64encode(body(name)).decode()}
        for name in files
    ]


def run(conn, source: dict, files: list[str], day: str, *, run_id: str | None = None) -> list[dict]:
    adapter = InsuranceAdapter(source, transport=fixture_transport(pages(files)))
    projector = InsuranceProjector(conn)
    receipts, cursor, page_no = [], None, 0
    while True:
        page = adapter.fetch_page({"operation": "documents", "parameters": {}, "limit": 5000}, cursor=cursor)
        page_no += 1
        projector.project_page(
            run_id=run_id or f"run:{source['source_id']}:{day}",
            manifest={},
            source=source,
            records=page.records,
            documents=[{"ingested_at": ms(day, 12) + page_no}],
            page_receipt=dict(page.receipt),
            principal_id=PRINCIPAL,
        )
        receipts.append({**dict(page.receipt), "records": list(page.records)})
        cursor = page.next_cursor
        if cursor is None:
            return receipts


def acquire(conn, key: str, day: str, *, run_id: str | None = None) -> list[dict]:
    files = STAGES[key][day]
    return run(conn, fictional(key, files), files, day, run_id=run_id)


def acquire_all(conn, *, until: str | None = None) -> None:
    for day, key in sorted((day, key) for key, stages in STAGES.items() for day in stages):
        if until is None or day <= until:
            acquire(conn, key, day)


def connection():
    return duckdb.connect(":memory:")


# ------------------------------------------------------------------ counterparts in other owners


def lei_records(conn, *, parent: bool = True) -> None:
    """GLEIF-style records for the fictional group and its subsidiary, with the subsidiary's parent links."""
    from src.kb.lei import LeiStore

    store = LeiStore(conn, now=lambda: ms("2025-01-01"))
    records = []
    for lei, name in ((GROUP_LEI, GROUP), (SOLO_LEI, SOLO)):
        records.append({"lei_record": {
            "lei": lei, "part": "record", "raw_sha256": hashlib.sha256(lei.encode()).hexdigest(),
            "attributes": {"lei": lei, "entity": {"legalName": {"name": name}, "jurisdiction": "DE"},
                           "registration": {"status": "ISSUED", "lastUpdateDate": "2025-01-01T00:00:00Z"}}}})
    if parent:
        for level in ("direct", "ultimate"):
            records.append({"lei_record": {
                "contract": "noesis-lei-part-v1", "provider": "gleif", "lei": SOLO_LEI,
                "part": f"{level}-parent-relationship", "raw_sha256": hashlib.sha256(level.encode()).hexdigest(),
                "attributes": {"relationship": {"endNode": {"id": GROUP_LEI}, "status": "ACTIVE", "periods": []}}}})
    store.observe_page(LEI_NS, records, run_id="lei-fixture", page_receipt={"part": "record"})


def ownership_entities(conn) -> None:
    """Register-side records: the group's GLEIF record (LEI), a register record for the subsidiary without an LEI,
    a same-named record in another country and a same-named record without a jurisdiction."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    OwnershipStore(conn, now=lambda: ms("2025-01-01")).apply(
        OWN_NS,
        [
            record("legal_entity", f"lei:{GROUP_LEI}", {"provider": "gleif", "provider_record_id": GROUP_LEI},
                   name=GROUP, jurisdiction="DE", identifiers=[{"scheme": "lei", "value": GROUP_LEI}]),
            record("legal_entity", "register:fiktiva-leben", {"provider": "handelsregister",
                                                              "provider_record_id": "fiktiva-leben"},
                   name=SOLO, jurisdiction="DE", identifiers=[{"scheme": "register", "value": "HRB 000002"}]),
            record("legal_entity", "register:fiktiva-leben-at", {"provider": "handelsregister",
                                                                 "provider_record_id": "fiktiva-leben-at"},
                   name=SOLO, jurisdiction="AT", identifiers=[{"scheme": "register", "value": "FN 000003"}]),
            record("legal_entity", "register:fiktiva-leben-unknown", {"provider": "opencorporates",
                                                                      "provider_record_id": "fiktiva-leben-x"},
                   name=SOLO, identifiers=[{"scheme": "register", "value": "X-000004"}]),
        ],
        run_id="register-run",
        observed_at_ms=ms("2025-01-01"),
        principal_id=PRINCIPAL,
    )


def hazard_events(conn) -> dict:
    """A fictional NHC storm matching the estimates' storm ID, and a GDACS event naming it without an identifier."""
    from src.kb import hazards_records as hr
    from src.kb.hazards_store import HazardStore

    storm = hr.event("nhc", "AL992024", "Hurricane Fiktiva", hazard_type="tropical_cyclone",
                     source_url="https://www.nhc.noaa.gov/fixture/al992024", revision_key="12",
                     published_at="2024-09-26T21:00:00Z", event_time="2024-09-26T18:00:00Z", parameters=[],
                     identifiers={"storm_id": "AL992024", "name": "Fiktiva"})
    other = hr.event("gdacs", "1000999", "Tropical Cyclone BEISPIEL-24", hazard_type="tropical_cyclone",
                     source_url="https://www.gdacs.org/fixture/1000999", revision_key="1",
                     published_at="2024-08-01T12:00:00Z", event_time="2024-08-01T06:00:00Z", parameters=[],
                     identifiers={"eventtype": "TC", "eventid": "1000999", "name": "Beispiel"})
    store = HazardStore(conn, now=lambda: ms("2024-10-10"))
    store.apply(HAZ_NS, [storm, other], run_id="hazard-fixture", principal_id=PRINCIPAL, scopes=SCOPES)
    return {
        "storm": store.find(HAZ_NS, "nhc", "hazard_event", "AL992024"),
        "other": store.find(HAZ_NS, "gdacs", "hazard_event", "1000999"),
    }


# ------------------------------------------------------------------ production fixtures


PRODUCTION_FILES = {
    "eiopa": ["eiopa_production_fixture.csv"],
    "sfcr": ["sfcr_group_2024.pdf", "sfcr_group_2024_corrected.pdf"],
    "naic": ["naic_market_share_2024.pdf"],
    "florida": ["florida_oir_claims.csv"],
    "ncei": ["ncei_billion_dollar.csv"],
}
NAMES = {"eiopa": "eiopa", "sfcr": "sfcr-allianz", "naic": "naic", "florida": "florida-oir", "ncei": "ncei"}


def production_pages(key: str) -> list[dict]:
    """Native pages for the production URLs; bodies are the fictional documents, so every row is out of scope."""
    source = production(key)
    result = []
    for document, name in zip(source["insurance"]["documents"], PRODUCTION_FILES[key], strict=True):
        parts = urlsplit(document["url"])
        raw = body(name)
        if key == "eiopa":
            raw = make_xlsx(raw.decode())
        result.append({"request": parts.path + ("?" + parts.query if parts.query else ""), "status": 200,
                       "body_base64": base64.b64encode(raw).decode(), "authored": True})
    return result


def write_production_fixtures() -> dict:
    from src.ingestion.insurance_sources import replay_native_fixture

    manifest = json.loads(PACK.read_text())
    hashes = {}
    for key, name in NAMES.items():
        fixture = {
            "captured": False,
            "provider": SOURCE_IDS[key],
            "note": "Authored documents naming fictional insurers and events only, served for the production "
            "document URLs. The production scope (audited countries, insurer sample and events, verify) therefore "
            "excludes their rows, the NAIC source records a publication reference only, and parsing is "
            "exercised with the fictional scope in tests/unit/insurance_harness.py.",
            "native_pages": production_pages(key),
            "scenarios": ["bounded-scope", "out-of-scope-rows-counted", "licence-decision-enforced"],
        }
        path = ROOT / f"tests/fixtures/source_packs/market-insurance-{name}.json"
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        raw = path.read_bytes()
        entry = next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key])
        entry["fixture"]["path"] = str(path.relative_to(ROOT))
        entry["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()
        entry["fixture"]["expected_output_hash"] = "0" * 64
        PACK.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        output = replay_native_fixture(production(key), json.loads(raw))
        entry["fixture"]["expected_output_hash"] = _digest(output)
        hashes[key] = {**entry["fixture"], "records": len(output)}
    PACK.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return hashes


if __name__ == "__main__":
    write_pdf_fixtures()
    print(json.dumps(write_production_fixtures(), indent=2))
