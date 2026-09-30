"""Registrations linked to Legal works and Science papers only by explicit citation (#2224, SO09)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.astronomy_records import AstronomyError
from src.kb.astronomy_registration import RegistrationCitations
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h

NS = ah.NS
SCOPES = ah.SCOPES | {"knowledge:legal:read"}


def seed_legal(conn, identifier="UNTS-1023-15", title="Convention on Registration of Objects Launched into Outer "
               "Space (fictional legal record)"):
    from src.kb.legal import LegalStore

    LegalStore(conn)
    conn.execute(
        "INSERT INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ["work:registration-convention", "global", "cellar", "INT", "treaty", "treaty", identifier,
         json.dumps({"unts": identifier}), "United Nations", title, "run", 1],
    )


def seed_paper(conn, doi):
    from services.ingest.common.document_model import Document
    from src.ingestion.document_store import DocumentStore

    document = Document(document_id="astro-doc:reg1", source_type="paper", source_id="crossref", language="en",
                        ingested_at=1, url=f"https://doi.org/{doi}", title="Transfer of FICTSAT 1 (fictional)",
                        content="fictional", authors=["A. Fictional"],
                        metadata={"content_representation": "plain-text-abstract", "doi": doi})
    assert not DocumentStore(conn).upsert([document.to_dict()]).invalid


@pytest.fixture
def conn():
    value = duckdb.connect(":memory:")
    h.acquire_all(value, discos=False)
    yield value
    value.close()


def test_links_cite_the_instrument_named_in_the_document_and_papers_by_doi(conn):
    seed_legal(conn)
    seed_paper(conn, "10.5555/fict.2099.reg1")
    result = RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")
    assert result["skipped"] == []
    links = result["links"]
    legal = [link for link in links if link["target_kind"] == "legal-work"]
    assert {(link["citation_value"], link["state"]) for link in legal} == {
        ("UNTS-1023-15", "linked"), ("FL-SAA-2090", "unresolved")}
    linked = next(link for link in legal if link["state"] == "linked")
    assert linked["target_id"] == "work:registration-convention" and linked["basis"] == "stated-instrument-identifier"
    assert "Convention on Registration" in linked["stated"]
    paper = next(link for link in links if link["target_kind"] == "paper")
    assert paper["state"] == "linked" and paper["target_id"] == "astro-doc:reg1"
    # Idempotent.
    assert RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES,
                                            legal_namespace="global")["created"] == []


def test_no_keyword_links_a_title_alone_never_links(conn):
    seed_legal(conn, identifier="OTHER-ID")  # same title, a different identifier
    result = RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")
    assert not any(link["state"] == "linked" for link in result["links"] if link["target_kind"] == "legal-work")


def test_absent_packs_are_skipped_cleanly(conn):
    result = RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")
    assert {s["pack"] for s in result["skipped"]} == {"legal", "science"}
    assert result["links"] == []
    no_ns = RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES)
    assert any(s["pack"] == "legal" for s in no_ns["skipped"])


def test_legal_linking_needs_the_legal_read_scope_and_reverts_are_final(conn):
    seed_legal(conn)
    with pytest.raises(AstronomyError):
        RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=ah.SCOPES, legal_namespace="global")
    citations = RegistrationCitations(conn)
    links = citations.link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")["links"]
    linked = next(link for link in links if link["state"] == "linked")
    reverted = citations.revert(NS, linked["link_id"], "wrong instrument", principal_id="reviewer", scopes=SCOPES)
    assert reverted["state"] == "reverted"
    assert citations.link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")["created"] == []
    same = [link for link in citations.links(NS, scopes=SCOPES, record_id=linked["record_id"])
            if link["citation_value"] == "UNTS-1023-15"]
    assert [link["state"] for link in same] == ["reverted"]
