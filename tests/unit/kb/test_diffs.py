"""Unit tests for the diff(domain, since) change-feed primitive."""

import duckdb
import pytest

from src.kb import load_registry
from src.kb.claim_links import run_claim_linking_pass
from src.kb.clusters import run_clustering_pass
from src.kb.entities import run_entity_canonicalization_pass
from src.kb.membership import run_membership_pass
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    DAY_MS,
    CONTRA,
    DUP_A,
    DUP_B,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)
from tests.unit.kb.test_namespace_backing import CONFIG as NS_CONFIG


DAY1 = BASE_MS
DAY2 = BASE_MS + DAY_MS
# BASE_MS = 2025-06-15T15:06:40Z; the boundary between day 1 and day 2:
SINCE_DAY2 = "2025-06-16T00:00:00Z"


@pytest.fixture()
def conn():
    return duckdb.connect()


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "domains.yml"
    path.write_text(NS_CONFIG)
    return path


def _consolidate(conn):
    run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
    run_clustering_pass(conn)
    run_entity_canonicalization_pass(conn)


def _actor(conn, doc_id, name):
    conn.execute(
        "INSERT INTO document_actors (document_id, source_type, actor_name,"
        " role, confidence) VALUES (?, 'news', ?, 'subject', 0.9)",
        [doc_id, name],
    )


class TestCorpusDiff:
    def _timeline(self, conn, config_path):
        """Day 1: one story. Day 2: a corroborating copy, a contradiction,
        and a brand-new story."""
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", DAY1, "web3"),
                ("c2", DUP_B, "d2", DAY2, "web3"),
                ("c3", CONTRA, "d3", DAY2, "web3"),
                ("n1", "Mpox outbreak continues in region.", "d4", DAY2, "web3"),
            ],
        )
        conn.execute("UPDATE documents SET source_id = 'wire-a' WHERE document_id IN ('d1','d3')")
        conn.execute("UPDATE documents SET source_id = 'wire-b' WHERE document_id IN ('d2','d4')")
        _consolidate(conn)
        return load_registry(config_path).resolve("web3", conn=conn)

    def test_documents_section_distinguishes_new_from_total(self, conn, config_path):
        backing = self._timeline(conn, config_path)
        diff = backing.diff(since=SINCE_DAY2)
        assert diff["documents"]["new"] == 3
        assert diff["documents"]["total"] == 4
        assert diff["documents"]["sources_delivered"] == {"wire-a": 1, "wire-b": 2}

    def test_new_vs_gained_clusters(self, conn, config_path):
        backing = self._timeline(conn, config_path)
        diff = backing.diff(since=SINCE_DAY2)
        new_ids = {c["cluster_id"] for c in diff["new_clusters"]}
        # c3 and n1 are new stories; the c1/c2 cluster existed on day 1.
        assert "cl-c1" not in new_ids
        assert {"cl-c3", "cl-n1"} <= new_ids

        gained = {c["cluster_id"]: c for c in diff["gained_corroboration"]}
        assert "cl-c1" in gained
        assert gained["cl-c1"]["new_sources"] == ["wire-b"]

    def test_new_contradictions_cited_both_sides(self, conn, config_path):
        backing = self._timeline(conn, config_path)
        diff = backing.diff(since=SINCE_DAY2)
        assert len(diff["new_contradictions"]) >= 1
        entry = diff["new_contradictions"][0]
        assert entry["claim_a"]["text"] and entry["claim_b"]["text"]
        assert entry["prediction_mode"] == "zero-shot:fake-model"
        assert entry["confidence"] is not None

    def test_empty_diff_still_reports_coverage(self, conn, config_path):
        backing = self._timeline(conn, config_path)
        diff = backing.diff(since="2030-01-01")
        assert diff["documents"]["new"] == 0
        assert diff["documents"]["total"] == 4  # ingested, just nothing new
        assert diff["new_clusters"] == []
        assert diff["meta"]["consolidation"]["claim_links"] is not None

    def test_reproducible_for_fixed_since(self, conn, config_path):
        backing = self._timeline(conn, config_path)
        first = backing.diff(since=SINCE_DAY2)
        second = backing.diff(since=SINCE_DAY2)
        for key in ("documents", "new_clusters", "gained_corroboration",
                    "new_contradictions", "superseded", "entity_surges"):
            assert first[key] == second[key]

    def test_entity_surges_vs_trailing_baseline(self, conn, config_path):
        _seed_claims(conn, [("c1", DUP_A, "d1", DAY1, "web3")])
        # Baseline day: one mention. Window day: four mentions -> surge.
        docs = [("e1", DAY1), ("e2", DAY2), ("e3", DAY2), ("e4", DAY2), ("e5", DAY2)]
        for doc_id, when in docs:
            conn.execute(
                "INSERT INTO documents (document_id, source_type, ingested_at)"
                " VALUES (?, 'news', ?)",
                [doc_id, when],
            )
            conn.execute(
                "INSERT INTO document_domains VALUES (?, 'web3', 1.0, 'source', 'r0', 0)",
                [doc_id],
            )
            _actor(conn, doc_id, "Federal Reserve")
        # A steady entity: one mention each day, no surge.
        _actor(conn, "e1", "ECB")
        _actor(conn, "e2", "ECB")
        _consolidate(conn)

        backing = load_registry(config_path).resolve("web3", conn=conn)
        diff = backing.diff(since=SINCE_DAY2)
        surged = {surge["name"] for surge in diff["entity_surges"]}
        assert "Federal Reserve" in surged
        assert "ECB" not in surged

    def test_superseded_section(self, conn, config_path):
        _seed_claims(
            conn,
            [
                ("old", DUP_A, "d1", DAY1, "web3"),
                ("new", DUP_B, "d2", DAY1 + 5 * DAY_MS, "web3"),
            ],
        )
        conn.execute(
            "UPDATE documents SET source_id = 'same-wire' "
            "WHERE document_id IN ('d1', 'd2')"
        )
        _consolidate(conn)
        backing = load_registry(config_path).resolve("web3", conn=conn)
        diff = backing.diff(since=SINCE_DAY2)
        assert len(diff["superseded"]) == 1
        assert diff["superseded"][0]["superseded_claim"]["claim_id"] == "old"


class TestNamespaceDiff:
    def test_same_shape_with_honest_gaps(self, conn, config_path):
        from src.provisioning.namespaces import create_namespace

        from src.kb.claim_links import ensure_claim_link_schema

        ensure_claim_link_schema(conn)
        tables = create_namespace(conn, "reference")
        conn.execute(
            f"INSERT INTO {tables['documents']} (id, title, source, source_type,"
            " url, published_at, routed_at) VALUES ('p1', 'Paper', 'arxiv',"
            " 'paper', 'https://x/p1', NULL, now())"
        )
        conn.execute(
            f"INSERT INTO {tables['claims']} (claim_id, claim_text, verdict,"
            " document_id, routed_at) VALUES ('pc1', 'A finding.', NULL, 'p1', now())"
        )
        backing = load_registry(config_path).resolve("reference", conn=conn)
        diff = backing.diff(since="2020-01-01")
        assert diff["documents"]["new"] == 1
        assert diff["new_clusters"][0]["representative"]["claim_id"] == "pc1"
        assert diff["entity_surges"] is None  # explicit gap, not silent []
        for key in ("documents", "new_clusters", "gained_corroboration",
                    "new_contradictions", "superseded", "meta"):
            assert key in diff
