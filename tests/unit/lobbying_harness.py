"""Offline harness for the Political lobbying feature (#1911): authored register exports replayed through the real adapter.

Every file under ``tests/fixtures/lobbying`` is authored in the register's
documented shape and names fictional organisations and people only; nothing
here is live coverage. Exports go through :class:`LobbyingRegisterAdapter` (the
connector the runtime compiles) and :class:`LobbyingProjector`. Legislative
dossiers are built by :class:`LegislativeDossierStore` from committed document
revisions, as the Political pack does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.lobbying_sources import LobbyingRegisterAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.lobbying import LobbyingProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/lobbying"
PACK = ROOT / "config/source_packs/political.json"
NS = "global"
DOSSIER_NS = "research"
SCOPES = {
    "knowledge:political:lobbying:read",
    "knowledge:political:lobbying:write",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:companies:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {
    "knowledge:ownership:review",
    "knowledge:political:lobbying:review",
}
READ_ONLY = {"knowledge:political:lobbying:read", f"namespace:{NS}:read"}
SOURCES = {
    "eu-tr": "eu-transparency-register",
    "de-lobbyregister": "de-lobbyregister",
    "ep-meetings": "ep-mep-meetings",
    "ec-meetings": "ec-meetings",
    "uk-orcl": "uk-consultant-lobbyists",
}
FILES = {
    "eu-tr": ["eu_tr_2099-01-15.xml", "eu_tr_2099-03-01.xml"],
    "de-lobbyregister": ["de_lobbyregister_2099-02-01.json"],
    "ep-meetings": ["ep_meetings_2099-02-10.csv"],
    "ec-meetings": ["ec_meetings_2099-02-12.json"],
    "uk-orcl": ["uk_orcl_2099-04-30.csv"],
}
EU_ASSOC = ("eu-tr", "000000000101-01")
EU_CONSULTANCY = ("eu-tr", "000000000202-02")
EU_FORUM = ("eu-tr", "000000000303-03")
DE_ASSOC = ("de-lobbyregister", "R009901")
DE_CONSULTANCY = ("de-lobbyregister", "R009902")
UK_CONSULTANCY = ("uk-orcl", "ORCL0099")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == source_id)
    )


def page(
    register: str,
    filename: str,
    *,
    limit: int = 1000,
    final_url: str | None = None,
    body: str | None = None,
    item: dict | None = None,
):
    item = item or source(SOURCES[register])
    parts = urlsplit(item["endpoint"])
    key = parts.path + ("?" + parts.query if parts.query else "")
    native = {
        "request": key,
        "status": 200,
        "body": body if body is not None else (FIXTURES / filename).read_text(),
        **({"final_url": final_url} if final_url else {}),
    }
    adapter = LobbyingRegisterAdapter(item, transport=fixture_transport([native]))
    return adapter.fetch_page(
        {"operation": "export", "parameters": {}, "limit": limit}, cursor=None
    ), item


def apply(
    conn,
    register: str,
    filename: str,
    *,
    run_id: str | None = None,
    body: str | None = None,
    item: dict | None = None,
) -> dict:
    fetched, item = page(register, filename, body=body, item=item)
    return LobbyingProjector(conn).project_page(
        run_id=run_id or f"run:{filename}",
        manifest=None,
        source=item,
        records=fetched.records,
        documents=[],
        page_receipt=fetched.receipt,
        principal_id="operator",
    )[0]


def load_all(conn) -> None:
    for register, files in FILES.items():
        for name in files:
            apply(conn, register, name)


def entry_id(conn, key: tuple[str, str]) -> str:
    return conn.execute(
        "SELECT entry_id FROM lobbying_entries WHERE register=? AND native_id=?",
        list(key),
    ).fetchone()[0]


class Clock:
    def __init__(self, start: int = 4_080_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def connection():
    return duckdb.connect(":memory:")


# ------------------------------------------------------------------ legislative dossiers


DOSSIER_SCOPES = {
    "knowledge:political:dossier:read",
    "knowledge:political:dossier:write",
    f"namespace:{DOSSIER_NS}:read",
    f"namespace:{DOSSIER_NS}:write",
}


def _documents(conn):
    from src.ingestion.connectors.political_official import PoliticalOfficialConnector
    from src.ingestion.document_store import DocumentStore

    documents = list(
        PoliticalOfficialConnector().harvest(
            {
                "offline": True,
                "source_ids": ["de-bundestag-dip", "eu-eurlex-regulatory"],
            }
        )
    )
    for doc in documents:
        doc.ingested_at = 1000
    store = DocumentStore(conn)
    store.upsert(documents)
    de, eu = (d.to_dict() for d in documents)
    return store, de, eu


def _add(
    store,
    template,
    *,
    source_id,
    identity,
    document_type,
    title,
    content,
    political,
    observed_at,
):
    payload = dict(template)
    metadata = dict(payload["metadata"])
    metadata.update(
        {
            "document_type": document_type,
            "official_identifier": identity,
            "political": json.dumps(political),
        }
    )
    payload.update(
        {
            "document_id": f"political:{source_id}:{identity}",
            "url": f"https://example.invalid/{source_id}/{identity.replace('/', '-')}",
            "title": title,
            "content": content,
            "metadata": metadata,
            "ingested_at": observed_at,
        }
    )
    assert store.upsert([payload]).invalid == 0
    return payload["document_id"]


def _ref(conn, document_id):
    revision_id = conn.execute(
        "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
        [document_id],
    ).fetchone()[0]
    return {"document_id": document_id, "revision_id": revision_id}


def dossiers(conn, *, now=None):
    """An EU dossier on procedure 2099/0101(COD) and a DE dossier on printed paper 21/9901 (fictional)."""
    from src.domains.political.legislative_dossiers import LegislativeDossierStore

    store, de, eu = _documents(conn)
    eu_doc = _add(
        store,
        eu,
        source_id="eu-eurlex-regulatory",
        identity="COM-2099-0101",
        document_type="proposal",
        title="Proposal for a Regulation on fictional grid tariffs",
        content="Proposal for a Regulation on fictional grid tariffs (fixture).",
        political={"procedure_id": "2099/0101-cod", "fixture": True},
        observed_at=2000,
    )
    de_doc = _add(
        store,
        de,
        source_id="de-bundestag-dip",
        identity="21/9901",
        document_type="proposal",
        title="Entwurf eines fiktiven Netzentgeltgesetzes",
        content="Gesetzentwurf der Bundesregierung (fiktiv).",
        political={
            "procedure_id": "proposal:de:fiktives-netzentgeltgesetz",
            "fixture": True,
        },
        observed_at=2000,
    )
    scopes = DOSSIER_SCOPES | {f"document:{d}:read" for d in (eu_doc, de_doc)}
    dossier = LegislativeDossierStore(conn, now=now or (lambda: 3000))
    eu_saved = dossier.save(
        DOSSIER_NS,
        "grid-tariffs",
        "EU",
        "2099/0101-cod",
        [_ref(conn, eu_doc)],
        principal_id="alice",
        scopes=scopes,
    )
    de_saved = dossier.save(
        DOSSIER_NS,
        "netzentgelt",
        "DE",
        "proposal:de:fiktives-netzentgeltgesetz",
        [_ref(conn, de_doc)],
        principal_id="alice",
        scopes=scopes,
    )
    return {
        "eu": eu_saved,
        "de": de_saved,
        "scopes": scopes,
        "documents": {"eu": eu_doc, "de": de_doc},
        "store": store,
        "templates": {"eu": eu, "de": de},
    }
