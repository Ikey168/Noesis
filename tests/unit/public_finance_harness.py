"""Offline harness for the Economics public-finance feature (#1909): authored files replayed through the real adapter.

Every file under ``tests/fixtures/public_finance`` is authored in the provider's
documented shape and names fictional budget lines, beneficiaries and figures
only (the Eurostat cube states fictional values and says so in its label);
nothing here is live coverage. Files go through
:class:`PublicFinanceAdapter` (the connector the runtime compiles) and
:class:`PublicFinanceProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.public_finance_sources import (
    PublicFinanceAdapter,
    document_url,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.public_finance import PublicFinanceProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/public_finance"
PACK = ROOT / "config/source_packs/economic.json"
NS = "global"
READ = "knowledge:economic:public-finance:read"
WRITE = "knowledge:economic:public-finance:write"
REVIEW = "knowledge:economic:public-finance:review"
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:economic:read",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {REVIEW, "knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
SOURCES = {
    "bund": "bundeshaushalt-open-data",
    "berlin": "berlin-haushalt",
    "fts": "eu-financial-transparency-system",
    "gfs": "eurostat-government-finance",
}
BUND_FILES = (
    "de_bund_2099_soll.csv",
    "de_bund_2099_nachtrag1.csv",
    "de_bund_2099_ist_vorlaeufig.csv",
    "de_bund_2099_haushaltsrechnung.csv",
)
BERLIN = "de_be_2099_2100.csv"
FTS = "eu_fts_2099.csv"
FTS_LAST_MODIFIED = "Wed, 30 Jun 2100 10:00:00 GMT"
GFS_APRIL = "eurostat_gov_10a_main_2025-04.json"
GFS_OCTOBER = "eurostat_gov_10a_main_2025-10.json"
# Fictional citations an operator would declare from each plan's title page (#1968).
PLAN_REFERENCES = [
    {
        "scheme": "de-bgbl",
        "value": "BGBl. 2098 I Nr. 999",
        "role": "Haushaltsgesetz 2099",
    },
    {"scheme": "de-drucksache", "value": "99/1001", "role": "Regierungsentwurf"},
]
NACHTRAG_REFERENCES = [
    {"scheme": "de-drucksache", "value": "99/1002", "role": "Nachtragshaushaltsgesetz"}
]
BERLIN_REFERENCES = [
    {
        "scheme": "de-be-gvbl",
        "value": "GVBl. 2098 S. 999",
        "role": "Haushaltsgesetz 2099/2100",
    }
]


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(key: str) -> dict:
    item = copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[key])
    )
    documents = item["public_finance"]["documents"]
    if key == "bund":
        documents[0]["references"] = PLAN_REFERENCES
        documents[1]["references"] = NACHTRAG_REFERENCES
    if key == "berlin":
        documents[0]["references"] = BERLIN_REFERENCES
    return item


def body(filename: str) -> str:
    return (FIXTURES / filename).read_text()


def pages(
    key: str,
    files,
    *,
    item: dict | None = None,
    headers: dict | None = None,
    final_url=None,
    limit: int = 100_000,
):
    """Every page of one source run: ``files`` are (document index, filename or body) pairs."""
    item = item or source(key)
    natives = []
    for index, name in files:
        document = item["public_finance"]["documents"][index]
        parts = urlsplit(document_url(document))
        text = body(name) if "\n" not in name and (FIXTURES / name).exists() else name
        natives.append(
            {
                "request": parts.path + ("?" + parts.query if parts.query else ""),
                "status": 200,
                "headers": headers
                if headers is not None
                else (
                    {"Last-Modified": FTS_LAST_MODIFIED}
                    if key == "fts"
                    else {"Content-Type": "text/csv"}
                ),
                "body": text,
                **({"final_url": final_url} if final_url else {}),
            }
        )
    adapter = PublicFinanceAdapter(item, transport=fixture_transport(natives))
    return adapter, item


def fetch(key: str, index: int, name: str, **kwargs):
    adapter, item = pages(key, [(index, name)], **kwargs)
    return adapter.fetch_page(
        {
            "operation": "release",
            "parameters": {},
            "limit": kwargs.get("limit", 100_000),
        },
        cursor=None if index == 0 else str(index),
    ), item


def apply(
    conn,
    key: str,
    index: int,
    name: str,
    *,
    run_id: str | None = None,
    item: dict | None = None,
    headers: dict | None = None,
    now=None,
) -> dict:
    fetched, item = fetch(key, index, name, item=item, headers=headers)
    projector = PublicFinanceProjector(conn)
    if now is not None:
        projector.store.now = now
    return projector.project_page(
        run_id=run_id or f"run:{name[:40]}",
        manifest=None,
        source=item,
        records=fetched.records,
        documents=[],
        page_receipt=fetched.receipt,
        principal_id="operator",
    )[0]


def load_budgets(conn) -> list[dict]:
    results = [
        apply(conn, "bund", index, name) for index, name in enumerate(BUND_FILES)
    ]
    results.append(apply(conn, "berlin", 0, BERLIN))
    results.append(apply(conn, "fts", 0, FTS))
    return results


def load_findings(conn, *, principal_id: str = "auditor") -> dict:
    from src.kb.public_finance import PublicFinanceStore

    sheet = json.loads(body("brh_bemerkungen_2100.json"))
    return PublicFinanceStore(conn).import_findings(
        NS, sheet, principal_id=principal_id, scopes=SCOPES
    )


def line_id(conn, scheme: str, **codes) -> str:
    from src.kb.public_finance import PublicFinanceStore

    (line,) = PublicFinanceStore(conn, initialize=False).lines(
        NS, scheme=scheme, codes=codes
    )
    return line["line_id"]


class Clock:
    def __init__(self, start: int = 1_900_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


LEGAL_NS = "legal"
DOSSIER_NS = "research"
PROCUREMENT_NS = "procurement"
LEGAL_SCOPES = {"knowledge:legal:read", f"namespace:{LEGAL_NS}:read"}
PROCUREMENT_SCOPES = {"knowledge:procurement:read", f"namespace:{PROCUREMENT_NS}:read"}


def load_acts(conn) -> dict:
    """A fictional Berlin budget act (berlin-law, GVBl reference) and a fictional EU budget act (CELLAR)."""
    from src.kb.legal import REGIONAL_CONTRACT, LegalStore

    records = [
        {
            "contract": REGIONAL_CONTRACT,
            "provider": "berlin-law",
            "provider_id": "jlr-HGBE2099pP1",
            "kind": "normative",
            "language": "de",
            "title": "Gesetz über die Feststellung des Haushaltsplans von Berlin für die Haushaltsjahre 2099 und "
            "2100 (Haushaltsgesetz 2099/2100, fiktiv)",
            "fields": {
                "gazette_reference": "GVBl. 2098 S. 999",
                "enactment_date": "2098-12-15",
            },
        },
        {
            "contract": REGIONAL_CONTRACT,
            "provider": "berlin-law",
            "provider_id": "jlr-NHGBE2099pP1",
            "kind": "normative",
            "language": "de",
            "title": "Nachtragshaushaltsgesetz 2099 für Berlin (fiktiv)",
            "fields": {
                "gazette_reference": "GVBl. 2099 S. 111",
                "enactment_date": "2099-07-01",
            },
        },
    ]
    return LegalStore(conn).project(
        LEGAL_NS, records, run_id="legal-fixture", source_id="berlin-law-fixture"
    )


def budget_dossiers(conn) -> dict:
    """Fictional DE dossiers: the 2099 budget bill (printed paper 99/1001) and a supplementary bill (99/1009)."""
    from src.domains.political.legislative_dossiers import LegislativeDossierStore
    from tests.unit import lobbying_harness as lh

    store, de, _eu = lh._documents(conn)
    bill = lh._add(
        store,
        de,
        source_id="de-bundestag-dip",
        identity="99/1001",
        document_type="proposal",
        title="Entwurf eines Gesetzes über die Feststellung des Bundeshaushaltsplans für das "
        "Haushaltsjahr 2099 (fiktiv)",
        content="Gesetzentwurf der Bundesregierung (fiktiv).",
        political={"procedure_id": "proposal:de:haushaltsgesetz-2099", "fixture": True},
        observed_at=2000,
    )
    nachtrag = lh._add(
        store,
        de,
        source_id="de-bundestag-dip",
        identity="99/1009",
        document_type="proposal",
        title="Entwurf eines Nachtragshaushaltsgesetzes 2099 (fiktiv)",
        content="Gesetzentwurf (fiktiv).",
        political={
            "procedure_id": "proposal:de:nachtragshaushaltsgesetz-2099",
            "fixture": True,
        },
        observed_at=2000,
    )
    scopes = lh.DOSSIER_SCOPES | {f"document:{d}:read" for d in (bill, nachtrag)}
    saved = LegislativeDossierStore(conn, now=lambda: 3000)
    budget = saved.save(
        DOSSIER_NS,
        "haushalt-2099",
        "DE",
        "proposal:de:haushaltsgesetz-2099",
        [lh._ref(conn, bill)],
        principal_id="alice",
        scopes=scopes,
    )
    supplementary = saved.save(
        DOSSIER_NS,
        "nachtrag-2099",
        "DE",
        "proposal:de:nachtragshaushaltsgesetz-2099",
        [lh._ref(conn, nachtrag)],
        principal_id="alice",
        scopes=scopes,
    )
    return {"budget": budget, "supplementary": supplementary, "scopes": scopes}


def load_awards(conn) -> dict:
    """A fictional TED award whose supplier states the FTS beneficiary's VAT number, and an unrelated award."""
    from src.kb.procurement_notices import ProcurementNoticeStore
    from src.kb.procurement_records import party, record

    buyer = party(
        "buyer",
        "Fiktive Beschaffungsstelle",
        identifiers=[{"scheme": "national", "id": "DE-FB-1"}],
        country="DE",
    )
    awards = [
        record(
            "ted",
            "award",
            "999001-2099",
            "PROC-2099-1",
            "Fictional evaluation services",
            source_url="https://ted.europa.eu/en/notice/-/detail/999001-2099",
            buyer=buyer,
            awards=[
                {
                    "award_id": "1",
                    "date": "2099-05-01",
                    "status": "active",
                    "suppliers": [
                        party(
                            "supplier",
                            "Beispiel Forschung GmbH",
                            country="DE",
                            identifiers=[{"scheme": "VAT", "id": "DE 999999999"}],
                        )
                    ],
                }
            ],
        ),
        record(
            "ted",
            "award",
            "999002-2099",
            "PROC-2099-2",
            "Fictional catering",
            source_url="https://ted.europa.eu/en/notice/-/detail/999002-2099",
            buyer=buyer,
            awards=[
                {
                    "award_id": "1",
                    "date": "2099-06-01",
                    "status": "active",
                    "suppliers": [
                        party(
                            "supplier",
                            "Beispiel Kantine GmbH",
                            country="DE",
                            identifiers=[{"scheme": "VAT", "id": "DE888888888"}],
                        )
                    ],
                }
            ],
        ),
    ]
    return ProcurementNoticeStore(conn).ingest(
        PROCUREMENT_NS,
        "ted",
        awards,
        observation_id="ted-fixture",
        observed_at_ms=1,
        scopes={"operator"},
    )
