"""Shared offline harness for the Technology internet-infrastructure tests (#2743): pinned fixtures, real adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.internet_infrastructure_sources import (
    FIXTURE_SECRET,
    InternetInfrastructureAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.internet_infrastructure_store import InternetInfrastructureProjector

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/technology-internet-infrastructure.json"
FIXTURES = ROOT / "tests/fixtures/internet_infrastructure"
NS = "global"
READ = "knowledge:technical:internet-infrastructure:read"
WRITE = "knowledge:technical:internet-infrastructure:write"
REVIEW = "knowledge:technical:internet-infrastructure:review"
SCOPES = {READ, WRITE, REVIEW, "knowledge:subscriptions:read", "knowledge:subscriptions:write",
          "knowledge:source-identity:read", "knowledge:vulnerabilities:read", "namespace:global:read",
          "namespace:global:write"}
READ_ONLY = {READ, "namespace:global:read"}
SOURCES = {"ripestat": "ripestat-routing", "peeringdb": "peeringdb-network", "rdap": "rdap-registrations",
           "crtsh": "crtsh-certificates", "ct": "ct-log-list"}
REVISIONS = {"ripestat": "ripestat_revision.json", "peeringdb": "peeringdb_revision.json",
             "rdap": "rdap_transfer.json", "ct": "ct_log_list_revision.json",
             "ripestat-deprecated": "ripestat_deprecated.json"}
ASN, PREFIX, DOMAIN = "AS64500", "192.0.2.0/24", "example.org"


def ms(day: str) -> int:
    value = datetime.fromisoformat(day)
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


# Retrieval clocks: every fixture's source time precedes the retrieval that reads it.
FIRST_RETRIEVAL = ms("2095-06-01T00:00:00")
SECOND_RETRIEVAL = ms("2097-06-01T00:00:00")


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(name: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])


def pages(name: str, revision: str | None = None) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[revision]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def adapter(name: str, *, revision: str | None = None, transport=None, now_ms: int = FIRST_RETRIEVAL,
            item=None, secret: str | None = FIXTURE_SECRET, **kwargs) -> InternetInfrastructureAdapter:
    return InternetInfrastructureAdapter(item or source(name),
                                         transport=transport or fixture_transport(pages(name, revision)),
                                         secret=secret, sleep=kwargs.pop("sleep", lambda _s: None),
                                         now_ms=lambda: now_ms, bootstrap_cache=kwargs.pop("bootstrap_cache", {}),
                                         **kwargs)


def fetch(name: str, **kwargs) -> list[list[dict[str, Any]]]:
    item = kwargs.get("item") or source(name)
    runner = adapter(name, **kwargs)
    out, cursor = [], None
    while True:
        page = runner.fetch_page({"operation": "selection", "parameters": {},
                                  "limit": item["budgets"]["max_results"]}, cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: str | None = None, retrieved_at_ms: int = FIRST_RETRIEVAL,
          **kwargs) -> list[dict[str, Any]]:
    """Project every page of a source (one unit per page), as the runtime would."""
    projector = InternetInfrastructureProjector(conn)
    projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision, now_ms=retrieved_at_ms, **kwargs):
        results += projector.project_page(run_id=f"run:{name}:{revision or 'first'}", manifest=None,
                                          source=source(name), records=records, documents=None,
                                          page_receipt=None, principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in ("ripestat", "peeringdb", "rdap", "ct"):
            apply(conn, name, revision=name, retrieved_at_ms=SECOND_RETRIEVAL)


def store(conn):
    from src.kb.internet_infrastructure_store import InternetInfrastructureStore

    return InternetInfrastructureStore(conn, now=lambda: SECOND_RETRIEVAL)


def one(conn, provider: str, object_kind: str, native_id: str | None = None) -> dict[str, Any]:
    found = [o for o in store(conn).objects(NS, provider=provider, object_kind=object_kind)
             if native_id is None or o["native_id"] == native_id]
    assert len(found) == 1, (provider, object_kind, native_id, found)
    return found[0]


def register_source_identity(conn, domain: str = DOMAIN, name: str = "Example News (fictional)") -> str:
    """An OSINT source identity carrying the domain as a native id (what the OSINT pack's vetting writes)."""
    from src.kb.source_identity import SourceIdentityStore

    identity = SourceIdentityStore(conn).register(
        NS, "publication", name, principal_id="op",
        scopes={"knowledge:source-identity:write", "namespace:global:write"},
        native_ids={"domain": domain}, observed_at_ms=FIRST_RETRIEVAL,
        producer={"name": "fixture", "version": "1"})
    return identity["source_id"]
