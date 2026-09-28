"""Offline harness for the Market ``bafin-notices`` feature (#2106): fictional fixtures through the real adapters.

Everything under ``tests/fixtures/bafin_notices`` is authored in the documented
export shapes (BF01) and names fictional issuers (Musterwerke AG,
``DE000MSTR014``), companies and persons; nothing here is live coverage. Pages
go through :class:`BafinNoticeAdapter` with the fixture transport and then
through :class:`BafinNoticeProjector` exactly as the source-pack runtime calls
it. ``python -m tests.unit.bafin_harness`` rewrites the pinned production
fixtures and their hashes in ``config/source_packs/market-bafin.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.domains.market.bafin_notices import BafinNoticeProjector
from src.ingestion.bafin_sources import BafinNoticeAdapter, fixture_transport
from src.ingestion.source_packs import _digest, validate_source_pack

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/bafin_notices"
PACK = ROOT / "config/source_packs/market-bafin.json"
NS = "market-bafin"
OWN_NS = "ownership"
ISSUER = "DE000MSTR014"
ISSUER_LEI = "529900MUSTERWERKE005"
OTHER_ISSUER = "DE000BSPL026"
FIKTIVA_INVEST_LEI = "529900FIKTIVAINV0004"
FIKTIVA_HOLDING_LEI = "529900FIKTIVAHOLD014"
PRINCIPAL = "analyst"
SCOPES = {
    "market:bafin:read",
    "market:bafin:write",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
    f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "market:instruments:read",
}
READ_ONLY = {"market:bafin:read", f"namespace:{NS}:read"}
SOURCE_IDS = {
    "voting": "bafin-voting-rights",
    "dealings": "bafin-managers-transactions",
    "shorts": "bundesanzeiger-net-short-positions",
    "company": "bafin-company-database",
    "warnings": "bafin-warnings-measures",
}
# The observation order of the authored files per stage (dates are the simulated observation days).
STAGES = {
    "voting": {
        "2026-03-10": ["voting_rights_2026-03-10.csv"],
        "2026-04-20": ["voting_rights_2026-04-20.csv"],
    },
    "dealings": {
        "2026-04-10": ["dealings_2026-04-10.csv"],
        "2026-06-01": ["dealings_2026-06-01.csv"],
    },
    "shorts": {
        "2026-04-10": [
            "short_positions_history_2026-04-10.csv",
            "short_positions_current_2026-04-10.csv",
        ],
        "2026-05-01": [
            "short_positions_history_2026-05-01.csv",
            "short_positions_current_2026-05-01.csv",
        ],
        "2026-06-01": ["short_positions_current_2026-06-01.csv"],
    },
    "company": {
        "2026-04-10": ["company_2026-04-10.csv"],
        "2026-07-01": ["company_2026-07-01.csv"],
    },
    "warnings": {"2026-05-15": ["warnings.rss", "measures.rss"]},
}
FICTIONAL_ISSUERS = [
    {"isin": ISSUER, "name": "Musterwerke AG"},
]


def ms(day: str, hour: int = 12) -> int:
    return int(
        datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp() * 1000
    )


def production(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(
        next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key])
    )


def _rehash(source: dict) -> dict:
    source.pop("source_hash", None)
    source["source_hash"] = _digest(source)
    return source


def _document(key: str, name: str) -> dict:
    listing = (
        "history"
        if "history" in name
        else "current"
        if "current" in name
        else "complete"
    )
    document = {
        "label": name,
        "url": f"https://{urlsplit(production(key)['endpoint']).hostname}/fixture/{name}",
        "listing": listing,
    }
    if key == "warnings":
        document.update(
            {
                "listing": "partial",
                "kind": "bafin_measure"
                if name.startswith("measures")
                else "bafin_warning",
                "category": "fixture",
            }
        )
    return document


def fictional(key: str, files: list[str]) -> dict:
    """The production source with the fictional issuer set, UTF-8 fixture documents and complete listings."""
    source = production(key)
    declared = source["bafin"]
    declared["encoding"] = "utf-8"
    if "issuers" in declared:
        declared["issuers"] = copy.deepcopy(FICTIONAL_ISSUERS)
    if key == "company":
        declared["bafin_ids"] = ["123456"]
    declared["documents"] = [_document(key, name) for name in files]
    return _rehash(source)


def pages(files: list[str]) -> list[dict]:
    return [
        {
            "request": f"/fixture/{name}",
            "status": 200,
            "body": (FIXTURES / name).read_text(),
        }
        for name in files
    ]


def acquire(
    conn, key: str, day: str, *, run_id: str | None = None, source: dict | None = None
) -> list[dict]:
    """Run one stage's documents through the adapter and the runtime projector at a simulated observation time."""
    files = STAGES[key][day]
    source = source or fictional(key, files)
    adapter = BafinNoticeAdapter(source, transport=fixture_transport(pages(files)))
    projector = BafinNoticeProjector(conn)
    receipts, cursor, page_no = [], None, 0
    while True:
        page = adapter.fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 5000}, cursor=cursor
        )
        page_no += 1
        documents = [{"ingested_at": ms(day, 12) + page_no}]
        projector.project_page(
            run_id=run_id or f"run:{key}:{day}",
            manifest={},
            source=source,
            records=page.records,
            documents=documents,
            page_receipt=dict(page.receipt),
            principal_id=PRINCIPAL,
        )
        receipts.append(dict(page.receipt))
        cursor = page.next_cursor
        if cursor is None:
            return receipts


def acquire_all(conn, *, until: str | None = None) -> None:
    """Every stage in observation-date order (optionally only those on or before ``until``)."""
    steps = sorted((day, key) for key, stages in STAGES.items() for day in stages)
    for day, key in steps:
        if until is None or day <= until:
            acquire(conn, key, day)


def connection():
    return duckdb.connect(":memory:")


# ------------------------------------------------------------------ production fixtures


def production_pages(key: str) -> list[dict]:
    """Native pages for the production document URLs; bodies are the fictional exports (all out of scope)."""
    source = production(key)
    files = {
        "voting": ["voting_rights_2026-04-20.csv"],
        "dealings": ["dealings_2026-04-10.csv"],
        "shorts": [
            "short_positions_history_2026-05-01.csv",
            "short_positions_current_2026-05-01.csv",
        ],
        "company": ["company_2026-04-10.csv"],
        "warnings": ["warnings.rss", "measures.rss"],
    }[key]
    result = []
    for document, name in zip(source["bafin"]["documents"], files, strict=True):
        parts = urlsplit(document["url"])
        page = {
            "request": parts.path + ("?" + parts.query if parts.query else ""),
            "status": 200,
            "body": (FIXTURES / name).read_text(),
            "authored": True,
        }
        if source["bafin"].get("encoding") == "windows-1252":
            page["body_encoding"] = "windows-1252"
        result.append(page)
    return result


def write_production_fixtures() -> dict:
    from src.ingestion.bafin_sources import replay_native_fixture

    names = {
        "voting": "voting-rights",
        "dealings": "managers-transactions",
        "shorts": "short-positions",
        "company": "company-database",
        "warnings": "warnings-measures",
    }
    manifest = json.loads(PACK.read_text())
    hashes = {}
    for key, name in names.items():
        fixture = {
            "captured": False,
            "provider": SOURCE_IDS[key],
            "note": "Authored exports naming fictional issuers, companies and persons only, served for the production "
            "document URLs. The production issuer set (DAX 40, verify) is therefore out of scope for every "
            "row and no notice is emitted for it; parsing is exercised with the fictional issuer set in "
            "tests/unit/bafin_harness.py.",
            "native_pages": production_pages(key),
            "scenarios": [
                "bounded-issuer-set",
                "out-of-scope-rows-counted",
                "declared-column-mapping",
            ],
        }
        path = ROOT / f"tests/fixtures/source_packs/market-bafin-{name}.json"
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        raw = path.read_bytes()
        source = production(key)
        output = replay_native_fixture(source, json.loads(raw))
        entry = next(
            s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key]
        )
        entry["fixture"] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "expected_output_hash": _digest(output),
        }
        hashes[key] = entry["fixture"]
    PACK.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return hashes


if __name__ == "__main__":
    print(json.dumps(write_production_fixtures(), indent=2))


# ------------------------------------------------------------------ counterparts in other owners

MARKET_NS = "market:bafin-test"


def instruments(conn, *, issuer_lei: str = ISSUER_LEI) -> None:
    """A fictional issuer and share in the Market instrument master, keyed by the issuer's ISIN and LEI."""
    from src.domains.market.entitlements import MarketEntitlementStore
    from src.domains.market.instruments import MarketInstrumentStore

    t0 = ms("2020-01-01")
    MarketEntitlementStore(conn, now=lambda: t0).put_entitlement(
        MARKET_NS,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=[
            "ingest",
            "read",
            "display",
            "retain",
            "derive",
            "cache",
            "evidence",
            "export",
            "redistribute",
        ],
        evidence_ref="synthetic-fixture:rights-review:v1",
        decision_ref="synthetic-fixture:reviewer-decision:v1",
        principal_id="operator:fixture-reviewer",
        scopes={"operator"},
        effective_at_ms=0,
    )
    store = MarketInstrumentStore(conn, now=lambda: t0)

    def ref(name):
        return {
            "source_ref_id": f"src:{name}",
            "provider": "fixture-only",
            "provider_object_id": name,
            "source_revision_id": f"fixture:{name}:1",
            "public_at_ms": t0,
            "source_snapshot_id": f"snapshot:{name}",
            "source_url": None,
            "retrieved_at_ms": t0,
            "content_hash": hashlib.sha256(name.encode()).hexdigest(),
            "license_id": "fixture-only",
            "entitlement_id": "entitlement:fixture",
        }

    store.put_issuer(
        MARKET_NS,
        issuer_id="issuer:musterwerke",
        kg_entity_id="kg:musterwerke",
        display_name="Musterwerke AG",
        identifiers=[
            {
                "scheme": "lei",
                "value": issuer_lei,
                "valid_from_ms": t0,
                "valid_to_ms": None,
                "source_ref_id": "src:issuer",
            }
        ],
        source_refs=[ref("issuer")],
        principal_id=PRINCIPAL,
        scopes={"operator"},
    )
    store.put_security(
        MARKET_NS,
        security_id="security:musterwerke-share",
        issuer_id="issuer:musterwerke",
        security_type="common_equity",
        share_class="Inhaberaktie",
        denomination_currency="EUR",
        identifiers=[
            {
                "scheme": "isin",
                "value": ISSUER,
                "valid_from_ms": t0,
                "valid_to_ms": None,
                "source_ref_id": "src:security",
            }
        ],
        source_refs=[ref("security")],
        principal_id=PRINCIPAL,
        scopes={"operator"},
    )


def ownership_entities(conn) -> None:
    """Fictional register-side records: a GLEIF-style LEI record and a register record without an LEI."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    gleif = {"provider": "gleif", "provider_record_id": FIKTIVA_INVEST_LEI}
    register = {
        "provider": "companies-house",
        "provider_record_id": "fictional-holding",
    }
    OwnershipStore(conn, now=lambda: ms("2026-01-01")).apply(
        OWN_NS,
        [
            record(
                "legal_entity",
                f"lei:{FIKTIVA_INVEST_LEI}",
                gleif,
                name="Fiktiva Invest GmbH",
                jurisdiction="DE",
                identifiers=[{"scheme": "lei", "value": FIKTIVA_INVEST_LEI}],
            ),
            record(
                "legal_entity",
                "register:fiktiva-holding",
                register,
                name="Fiktiva Holding SE",
                jurisdiction="DE",
                identifiers=[{"scheme": "register", "value": "HRB 000001"}],
            ),
        ],
        run_id="register-run",
        observed_at_ms=ms("2026-01-01"),
        principal_id=PRINCIPAL,
    )
