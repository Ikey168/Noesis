"""Unit tests for the reference domain: stand-up, book ingest, depth linkage."""

import io
import zipfile

import duckdb
import pytest
import yaml

from src.kb import load_registry
from src.kb.claim_links import run_cross_backing_link_pass
from src.kb.reference import (
    ingest_documents_into_namespace,
    stand_up_reference,
    sync_namespace_from_feeds,
)
from tests.unit.kb.test_claim_links import BASE_MS, DUP_A, CONTRA, FakeNLI, FakeProvider

CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: fake-embed
    tags: [web3]
    keywords: [defi, staking]
  - name: papers
    backing: corpus-view
    embedding_model: fake-embed
    tags: [papers, research]
"""


class FakeClaimDetector:
    @staticmethod
    def predict_text(text):
        from src.argument_mining.models import ClaimPrediction

        return ClaimPrediction(text, 0, True, 0.9)


def _minimal_epub() -> bytes:
    """A tiny EPUB the stdlib parser can read: two chapters."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Monetary History</dc:title>
    <dc:creator>A. Economist</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chap2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>""",
        )
        archive.writestr(
            "OEBPS/chap1.xhtml",
            "<html><head><title>Chapter One</title></head><body>"
            "<h1>Chapter One</h1><p>The central bank will not raise rates in"
            " September. Historical evidence shows restraint.</p></body></html>",
        )
        archive.writestr(
            "OEBPS/chap2.xhtml",
            "<html><head><title>Chapter Two</title></head><body>"
            "<h1>Chapter Two</h1><p>Inflation dynamics follow money supply"
            " with long and variable lags.</p></body></html>",
        )
    return buffer.getvalue()


@pytest.fixture()
def conn():
    return duckdb.connect()


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "domains.yml"
    path.write_text(CONFIG)
    return path


def _seed_corpus(conn, config_path):
    from src.ingestion.document_store import DocumentStore
    from src.kb.claim_links import ensure_claim_link_schema
    from src.kb.membership import run_membership_pass

    ensure_claim_link_schema(conn)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "news-1",
                "source_type": "news",
                "language": "en",
                "ingested_at": BASE_MS,
                "source_id": "wire",
                "url": "https://example.com/news-1",
                "title": "Rates decision",
                "content": "The central bank will raise rates in September.",
                "metadata": {"tags": ["web3"]},
            },
            {
                "document_id": "arxiv-1",
                "source_type": "blog",
                "language": "en",
                "ingested_at": BASE_MS,
                "source_id": "arXiv cs.AI",
                "url": "https://arxiv.org/abs/1",
                "title": "A preprint",
                "content": "We study staking equilibria.",
                "metadata": {"tags": ["papers"]},
            },
        ]
    )
    run_membership_pass(conn, load_registry(config_path))
    conn.execute(
        "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
        " source_type, confidence) VALUES ('news-c1', ?, 'news-1', 'news', 0.8)",
        [DUP_A],
    )


class TestStandUp:
    def test_promotion_carries_papers_membership(self, conn, config_path):
        _seed_corpus(conn, config_path)
        result = stand_up_reference(conn, config_path)
        assert result["documents_copied"] == 1  # arxiv-1, not the news doc

        registry = load_registry(config_path)
        assert registry.get("papers").backing == "namespace"
        backing = registry.resolve("papers", conn=conn)
        assert [d["document_id"] for d in backing.documents()] == ["arxiv-1"]

    def test_sync_carries_new_feed_arrivals(self, conn, config_path):
        _seed_corpus(conn, config_path)
        stand_up_reference(conn, config_path)
        from src.ingestion.document_store import DocumentStore

        DocumentStore(conn).upsert(
            [
                {
                    "document_id": "arxiv-2",
                    "source_type": "blog",
                    "language": "en",
                    "ingested_at": BASE_MS + 1000,
                    "source_id": "arXiv cs.CL",
                    "url": "https://arxiv.org/abs/2",
                    "title": "Another preprint",
                    "content": "New results on parsing.",
                    "metadata": {"tags": ["papers"]},
                }
            ]
        )
        result = sync_namespace_from_feeds(conn, config_path)
        assert result["copied"] == 1
        backing = load_registry(config_path).resolve("papers", conn=conn)
        ids = {d["document_id"] for d in backing.documents()}
        assert ids == {"arxiv-1", "arxiv-2"}
        # Idempotent.
        assert sync_namespace_from_feeds(conn, config_path)["copied"] == 0


class TestBookIngest:
    def test_epub_chapters_become_cited_namespace_documents(self, conn, config_path):
        from src.ingestion.connectors.book.connector import book_metadata_to_documents
        from src.ingestion.connectors.book.epub_parser import parse_epub

        _seed_corpus(conn, config_path)
        stand_up_reference(conn, config_path)

        meta = parse_epub(_minimal_epub(), file_path="/books/monetary.epub")
        documents = book_metadata_to_documents(meta, ingested_at=BASE_MS + 2000)
        assert len(documents) == 2  # one per chapter

        summary = ingest_documents_into_namespace(
            conn, config_path, documents, provider=FakeProvider(),
            embedding_model="fake-embed", claim_detector=FakeClaimDetector(),
        )
        assert summary["documents"] == 2
        assert summary["embedded"] == 2
        assert summary["claims"] >= 1  # heuristic detector finds chapter claims

        backing = load_registry(config_path).resolve("papers", conn=conn)
        hits = backing.search("variable lags")
        assert len(hits) == 1
        # Citation points into the section: title carries the chapter path.
        assert "›" in hits[0]["title"]

        # Re-ingest is a no-op.
        again = ingest_documents_into_namespace(
            conn, config_path, documents, provider=FakeProvider(),
            embedding_model="fake-embed", claim_detector=FakeClaimDetector(),
        )
        assert again["documents"] == 0 and again["skipped"] == 2

    def test_depth_linkage_book_contradicts_news(self, conn, config_path):
        _seed_corpus(conn, config_path)
        stand_up_reference(conn, config_path)
        # A book section whose claim contradicts the news claim.
        ingest_documents_into_namespace(
            conn,
            config_path,
            [
                {
                    "document_id": "book-sec-1",
                    "source_type": "book",
                    "title": "Monetary History › Chapter One",
                    "source_id": "isbn-1",
                    "url": "file:///books/monetary.epub#chapter-one",
                    "content": CONTRA,
                    "ingested_at": BASE_MS,
                }
            ],
            provider=FakeProvider(),
            embedding_model="fake-embed",
            extract_claims=False,
        )
        tables_claims = "kg_papers_claims"
        conn.execute(
            f"INSERT INTO {tables_claims} (claim_id, claim_text, verdict,"
            " document_id, routed_at) VALUES ('book-claim-1', ?, NULL,"
            " 'book-sec-1', now())",
            [CONTRA],
        )
        registry = load_registry(config_path)
        run_cross_backing_link_pass(
            conn, registry, provider=FakeProvider(), nli=FakeNLI(),
            embedding_model="fake-embed",
        )
        # Discoverable from the news side: a contradicts link into the corpus.
        row = conn.execute(
            "SELECT domain_a, claim_a, domain_b, claim_b FROM claim_links"
            " WHERE relation = 'contradicts'"
        ).fetchone()
        assert row is not None
        endpoints = {(row[0], row[1]), (row[2], row[3])}
        assert ("papers", "book-claim-1") in endpoints
        assert ("web3", "news-c1") in endpoints

    def test_coverage_reports_reference_lifecycle(self, conn, config_path):
        _seed_corpus(conn, config_path)
        stand_up_reference(conn, config_path)
        backing = load_registry(config_path).resolve("papers", conn=conn)
        coverage = backing.coverage()
        assert coverage["backing"] == "namespace"
        assert coverage["namespace"] == "papers"
        assert coverage["documents"] == 1
        assert coverage["embedding_model_mismatches"] == 0
