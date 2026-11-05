"""Shared offline harness for the Economics tax-transparency tests (#2889): pinned fixtures through the real adapter.

Every file under ``tests/fixtures/tax`` and ``tests/fixtures/source_packs/economic-tax-transparency-*`` is authored in
the documented shape as known to the author, with fictional values and fictional reference years (2091-2093); edition
release dates sit in 2024 and 2025 so runtime retrievals never precede them. Nothing here is live coverage. Responses go
through :class:`TaxTransparencyAdapter` (the connector the runtime compiles) and :class:`TaxProjector`.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.tax_sources import TaxTransparencyAdapter, fixture_transport
from src.kb.tax_store import TaxProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/tax"
PACK_PATH = ROOT / "config/source_packs/economic-tax-transparency.json"
NS = "global"
OWN_NS = "ownership"
SCOPES = {
    "knowledge:tax:read", "knowledge:tax:write", "knowledge:tax:review", "knowledge:ownership:read",
    "knowledge:ownership:write", "namespace:global:read", "namespace:global:write", f"namespace:{OWN_NS}:read",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:tax:read", "namespace:global:read"}
SOURCES = {"cbcr": "oecd-cts-cbcr", "rates": "oecd-cts-rates", "offshore": "eutax-offshore-wealth",
           "billionaire": "eutax-billionaire-etr"}
REVISIONS = {"cbcr": "oecd_cbcr_2025_edition.json", "rates": "oecd_rates_2025_edition.json",
             "offshore": "eutax_offshore_2025_update.json"}
FIRST_RETRIEVAL = 1_727_740_800_000  # 2024-10-01: after every 2024 edition
SECOND_RETRIEVAL = 1_759_276_800_000  # 2025-10-01: after every 2025 edition
HOLDINGS_LEI = "gleif:lei:213800EXAMPLAHOLDS95"


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK_PATH.read_text()))


def revision(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / REVISIONS[name]).read_text())


def source(name: str, *, revised: bool = False) -> dict[str, Any]:
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name]))
    if revised:
        item["tax_transparency"]["documents"] = revision(name)["documents"]
    return item


def pages(name: str, revised: bool = False) -> list[dict[str, Any]]:
    if revised:
        return revision(name)["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revised: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or source(name, revised=revised)
    adapter = TaxTransparencyAdapter(item, transport=transport or fixture_transport(pages(name, revised)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revised: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one edition per page) into the store, as the runtime would."""
    item = source(name, revised=revised)
    projector = TaxProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revised=revised, item=item):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revised else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in REVISIONS:
            apply(conn, name, revised=True, retrieved_at_ms=SECOND_RETRIEVAL)


def references() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "public_cbcr_references.json").read_text())["references"]


def record_references(conn, *, at_ms: int = SECOND_RETRIEVAL) -> dict[str, Any]:
    from src.kb.tax_store import TaxStore

    return TaxStore(conn, now=lambda: at_ms).record_company_reports(NS, references(), principal_id="operator",
                                                                    scopes=SCOPES)


def ownership(conn) -> None:
    """The Corporate Ownership fixtures (Exampla group) in the ownership namespace, reviewed (competition harness)."""
    from tests.unit import competition_harness as ch

    ch.ownership(conn)


def day_ms(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def series_by(conn, **match: Any) -> dict[str, Any]:
    """The one series whose provider, dataset, indicator code, jurisdiction or counterpart match."""
    from src.kb.tax_store import TaxStore

    def ok(series):
        checks = {"provider": series["provider"], "dataset": series["dataset"],
                  "indicator": series["indicator"]["code"], "jurisdiction": series["jurisdiction"]["code"],
                  "counterpart": series["counterpart"]["code"]}
        return all(checks[k] == v for k, v in match.items())

    found = [s for s in TaxStore(conn, initialize=False).find_series(NS) if ok(s)]
    assert len(found) == 1, (match, [s["native_key"] for s in found])
    return found[0]
