"""Offline harness for the Legal sanctions feature (#1907): authored list files replayed through the real adapters.

Every list file under ``tests/fixtures/sanctions`` is authored in the provider's
documented format and names fictional parties only; nothing here is live
coverage. Snapshots go through :class:`SanctionsListAdapter` (the connector the
runtime compiles) and :class:`SanctionsProjector`; CELLAR fixtures go through
the real CELLAR adapter and :class:`LegalStore`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.legal_sources import replay_native_fixture as replay_legal
from src.ingestion.sanctions_sources import SanctionsListAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.legal import LegalStore
from src.kb.sanctions import SanctionsProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/sanctions"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
SCOPES = {
    "knowledge:legal:read",
    "knowledge:legal:write",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
SOURCES = {
    "eu": "eu-sanctions-consolidated",
    "un": "un-sc-consolidated",
    "ofac": "ofac-sls",
    "uk": "uk-sanctions-list",
}
FILES = {
    "eu": ["eu_fsf_2026-01-15.xml", "eu_fsf_2026-03-01.xml", "eu_fsf_2026-06-01.xml"],
    "un": ["un_sc_2026-03-10.xml", "un_sc_2026-06-10.xml"],
    "ofac": ["ofac_sdn_2026-03-05.xml", "ofac_sdn_2026-06-05.xml"],
    "uk": ["uk_sanctions_2026-03-15.xml", "uk_sanctions_2026-06-15.xml"],
}
FORBIDDEN = {
    "screening",
    "screening_result",
    "risk",
    "risk_score",
    "score",
    "compliance",
    "compliance_status",
    "verdict",
    "match_score",
    "sanctioned",
}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == source_id)
    )


def list_page(
    list_id: str, filename: str, *, limit: int = 1000, final_url: str | None = None
):
    item = source(SOURCES[list_id])
    from urllib.parse import urlsplit

    parts = urlsplit(item["endpoint"])
    key = parts.path + ("?" + parts.query if parts.query else "")
    page = {
        "request": key,
        "status": 200,
        "body": (FIXTURES / filename).read_text(),
        **({"final_url": final_url} if final_url else {}),
    }
    adapter = SanctionsListAdapter(item, transport=fixture_transport([page]))
    return adapter.fetch_page(
        {"operation": "records", "parameters": {}, "limit": limit}, cursor=None
    ), item


def apply(conn, list_id: str, filename: str, *, run_id: str | None = None) -> dict:
    page, item = list_page(list_id, filename)
    projector = SanctionsProjector(conn)
    return projector.project_page(
        run_id=run_id or f"run:{filename}",
        manifest=None,
        source=item,
        records=page.records,
        documents=[],
        page_receipt=page.receipt,
        principal_id="operator",
    )[0]


def load_legal(conn, source_id: str, *, run_id: str = "legal-fixture") -> dict:
    item = source(source_id)
    fixture = json.loads((ROOT / item["fixture"]["path"]).read_text())
    return LegalStore(conn).project(
        NS, replay_legal(item, fixture), run_id=run_id, source_id=source_id
    )


class Clock:
    def __init__(self, start: int = 1_780_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def connection():
    return duckdb.connect(":memory:")


def forbidden_keys(value, path="$"):
    """Every key anywhere in an answer that would carry a screening, risk or compliance result."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found
