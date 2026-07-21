"""Unit tests for the namespace backing, promotion round-trip, and cross-backing links."""

import duckdb
import pytest
import yaml

from src.kb import load_registry
from src.kb.claim_links import run_cross_backing_link_pass
from src.kb.membership import run_membership_pass
from src.kb.promotion import demote_to_corpus_view, promote_to_namespace
from src.kb.registry import DomainConfigError
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    DAY_MS,
    DUP_A,
    DUP_B,
    CONTRA,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)

CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: fake-embed
    tags: [web3]
    keywords: [defi, staking, stablecoin]
  - name: reference
    backing: namespace
    embedding_model: fake-embed
"""


@pytest.fixture()
def conn():
    return duckdb.connect()


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "domains.yml"
    path.write_text(CONFIG)
    return path


def _seed_corpus_domain(conn, config_path):
    from src.ingestion.document_store import DocumentStore
    from src.kb.claim_links import ensure_claim_link_schema

    ensure_claim_link_schema(conn)  # documents + membership + argument_claims
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "d1",
                "source_type": "news",
                "language": "en",
                "ingested_at": BASE_MS,
                "source_id": "wire",
                "url": "https://example.com/d1",
                "title": "Defi staking news",
                "content": "defi staking coverage continues.",
                "metadata": {"tags": ["web3"]},
            }
        ]
    )
    run_membership_pass(conn, load_registry(config_path))
    conn.execute(
        "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
        " source_type, confidence) VALUES ('c1', ?, 'd1', 'news', 0.8)",
        [DUP_A],
    )


class TestRegistryFields:
    def test_namespace_backend_defaults_and_validation(self, config_path):
        registry = load_registry(config_path)
        reference = registry.get("reference")
        assert reference.namespace == "reference"
        assert reference.namespace_backend == "table-prefix"

    def test_namespace_backend_invalid_value_rejected(self, tmp_path):
        path = tmp_path / "bad.yml"
        path.write_text(
            "version: 1\ndomains:\n"
            "  - {name: x, backing: namespace, embedding_model: m,"
            " namespace_backend: sqlite}\n"
        )
        with pytest.raises(DomainConfigError, match="namespace_backend"):
            load_registry(path)


class TestPromotionRoundTrip:
    def test_promote_flips_config_and_copies_rows(self, conn, config_path):
        _seed_corpus_domain(conn, config_path)
        result = promote_to_namespace(conn, "web3", config_path)
        assert result["documents_copied"] == 1
        assert result["claims_copied"] == 1

        flipped = yaml.safe_load(config_path.read_text())
        entry = next(d for d in flipped["domains"] if d["name"] == "web3")
        assert entry["backing"] == "namespace"
        assert entry["namespace"] == "web3"

        registry = load_registry(config_path)
        backing = registry.resolve("web3", conn=conn)
        assert backing.backing_type == "namespace"
        docs = backing.documents()
        assert [d["document_id"] for d in docs] == ["d1"]
        assert docs[0]["content"] == "defi staking coverage continues."

        clusters = backing.claims()
        assert clusters[0]["representative"]["claim_id"] == "c1"

    def test_same_consumer_calls_work_on_both_backings(self, conn, config_path):
        _seed_corpus_domain(conn, config_path)
        registry = load_registry(config_path)
        corpus_backing = registry.resolve("web3", conn=conn)
        corpus_docs = corpus_backing.documents()
        corpus_cov = corpus_backing.coverage()

        promote_to_namespace(conn, "web3", config_path)
        ns_backing = load_registry(config_path).resolve("web3", conn=conn)
        ns_docs = ns_backing.documents()
        ns_cov = ns_backing.coverage()

        # Same ids served, same core coverage keys answered.
        assert [d["document_id"] for d in ns_docs] == [
            d["document_id"] for d in corpus_docs
        ]
        for key in ("domain", "backing", "embedding_model", "ready", "documents"):
            assert key in corpus_cov and key in ns_cov
        assert corpus_cov["documents"] == ns_cov["documents"] == 1
        assert ns_backing.search("staking")[0]["document_id"] == "d1"

    def test_demote_restores_corpus_serving(self, conn, config_path):
        _seed_corpus_domain(conn, config_path)
        promote_to_namespace(conn, "web3", config_path)
        # Simulate a namespace-native document (never in the shared sink).
        conn.execute(
            "INSERT INTO kg_web3_documents (id, title, source, source_type, url,"
            " published_at, routed_at, content, ingested_at)"
            " VALUES ('native-1', 'Native doc', 'ns', 'paper', 'https://x/n1',"
            " NULL, now(), 'native content', ?)",
            [BASE_MS + DAY_MS],
        )
        result = demote_to_corpus_view(conn, "web3", config_path)
        assert result["documents_restored"] == 1

        registry = load_registry(config_path)
        backing = registry.resolve("web3", conn=conn)
        assert backing.backing_type == "corpus-view"
        ids = {d["document_id"] for d in backing.documents()}
        assert ids == {"d1", "native-1"}


class TestAttachedBackend:
    def test_attached_namespace_serves_and_joins(self, conn, config_path, tmp_path, monkeypatch):
        monkeypatch.setenv("NOESIS_KG_DB_DIR", str(tmp_path))
        _seed_corpus_domain(conn, config_path)
        promote_to_namespace(
            conn, "web3", config_path, backend="attached",
            db_path=str(tmp_path / "web3.duckdb"),
        )
        registry = load_registry(config_path)
        backing = registry.resolve("web3", conn=conn)
        assert backing.definition.namespace_backend == "attached"
        assert [d["document_id"] for d in backing.documents()] == ["d1"]
        # One engine: cross-database join between the attached namespace and
        # the shared corpus works in a single SQL statement.
        joined = conn.execute(
            "SELECT COUNT(*) FROM kg_web3.documents n"
            " JOIN argument_claims c ON c.document_id = n.id"
        ).fetchone()[0]
        assert joined == 1


class TestCrossBackingLinks:
    def _promote_and_add_native_claim(self, conn, config_path, text):
        _seed_corpus_domain(conn, config_path)
        # reference is already namespace-backed; create its tables + claim.
        from src.provisioning.namespaces import create_namespace

        tables = create_namespace(conn, "reference")
        conn.execute(
            f"INSERT INTO {tables['claims']} (claim_id, claim_text, verdict,"
            " document_id, routed_at) VALUES ('ref-c1', ?, NULL, 'ref-d1', now())",
            [text],
        )

    def test_native_claim_links_into_corpus(self, conn, config_path):
        self._promote_and_add_native_claim(conn, config_path, CONTRA)
        registry = load_registry(config_path)
        summary = run_cross_backing_link_pass(
            conn, registry, provider=FakeProvider(), nli=FakeNLI(),
            embedding_model="fake-embed",
        )
        assert summary["domains"]["reference"]["scanned"] == 1
        row = conn.execute(
            "SELECT domain_a, claim_a, domain_b, claim_b, relation"
            " FROM claim_links WHERE relation = 'contradicts'"
        ).fetchone()
        assert row is not None
        endpoints = {(row[0], row[1]), (row[2], row[3])}
        assert ("reference", "ref-c1") in endpoints
        assert ("web3", "c1") in endpoints

        # Visible from the namespace side's cluster shape too.
        backing = registry.resolve("reference", conn=conn)
        clusters = backing.claims()
        ref = next(c for c in clusters if c["representative"]["claim_id"] == "ref-c1")
        assert any(l["claim_id"] == "c1" for l in ref["contradictions"])

    def test_second_pass_scans_nothing(self, conn, config_path):
        self._promote_and_add_native_claim(conn, config_path, DUP_B)
        registry = load_registry(config_path)
        run_cross_backing_link_pass(
            conn, registry, provider=FakeProvider(), nli=FakeNLI(),
            embedding_model="fake-embed",
        )
        second = run_cross_backing_link_pass(
            conn, registry, provider=FakeProvider(), nli=FakeNLI(),
            embedding_model="fake-embed",
        )
        assert second["domains"]["reference"]["scanned"] == 0

    def test_embedding_space_mismatch_fails_loudly(self, conn, tmp_path):
        path = tmp_path / "mismatch.yml"
        path.write_text(
            CONFIG.replace(
                "  - name: reference\n    backing: namespace\n"
                "    embedding_model: fake-embed",
                "  - name: reference\n    backing: namespace\n"
                "    embedding_model: other-space",
            )
        )
        registry = load_registry(path)
        from src.kb.claim_links import ensure_claim_link_schema

        ensure_claim_link_schema(conn)
        with pytest.raises(DomainConfigError, match="shared embedding space"):
            run_cross_backing_link_pass(conn, registry, provider=FakeProvider())
