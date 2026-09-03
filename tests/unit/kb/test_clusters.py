"""Unit tests for claim clustering and the presentation-time merge."""

import duckdb
import pytest

from src.kb.claim_links import delete_run, run_claim_linking_pass
from src.kb.clusters import (
    cluster_claims,
    ensure_cluster_schema,
    run_clustering_pass,
)
from tests.unit.kb.test_claim_links import (
    CONTRA,
    DAY_MS,
    BASE_MS,
    DUP_A,
    DUP_B,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)


@pytest.fixture()
def conn():
    return duckdb.connect()


def _link_and_cluster(conn):
    summary = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
    run_clustering_pass(conn)
    return summary


class TestClustering:
    def test_duplicates_form_one_cluster(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, "economics"),
                ("c2", DUP_B, "d2", BASE_MS + DAY_MS, "economics"),
            ],
        )
        result = _link_and_cluster(conn)
        assert result["links"]["duplicate"] == 1
        stats = run_clustering_pass(conn)
        assert stats["clusters"] == 1
        rows = conn.execute(
            "SELECT DISTINCT cluster_id FROM claim_clusters"
        ).fetchall()
        assert rows == [("cl-c1",)]  # smallest member id wins

    def test_cluster_ids_stable_across_reruns(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("c2", DUP_B, "d2", BASE_MS, None),
            ],
        )
        _link_and_cluster(conn)
        before = dict(
            conn.execute("SELECT claim_id, cluster_id FROM claim_clusters").fetchall()
        )
        run_clustering_pass(conn)
        after = dict(
            conn.execute("SELECT claim_id, cluster_id FROM claim_clusters").fetchall()
        )
        assert before == after

    def test_self_heals_after_delete_run(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("c2", DUP_B, "d2", BASE_MS, None),
            ],
        )
        summary = _link_and_cluster(conn)
        assert conn.execute("SELECT COUNT(*) FROM claim_clusters").fetchone()[0] == 2

        delete_run(conn, summary["run_id"])
        stats = run_clustering_pass(conn)
        assert stats["linked_claims"] == 0
        assert conn.execute("SELECT COUNT(*) FROM claim_clusters").fetchone()[0] == 0


class TestPresentationMerge:
    def _seed_story(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, "economics"),
                ("c2", DUP_B, "d2", BASE_MS + DAY_MS, "economics"),
                ("c3", CONTRA, "d3", BASE_MS + DAY_MS, "economics"),
            ],
        )
        conn.execute("UPDATE documents SET source_id = 'reuters' WHERE document_id = 'd1'")
        conn.execute("UPDATE documents SET source_id = 'bbc' WHERE document_id = 'd2'")
        conn.execute("UPDATE documents SET source_id = 'ft' WHERE document_id = 'd3'")
        _link_and_cluster(conn)

    def test_merged_cluster_with_citations_and_corroboration(self, conn):
        self._seed_story(conn)
        clusters = cluster_claims(conn, domain="economics")
        merged = next(c for c in clusters if c["size"] == 2)
        assert merged["corroboration"] == 2
        assert {c["source"] for c in merged["citations"]} == {"reuters", "bbc"}
        assert merged["representative"]["claim_id"] in {"c1", "c2"}

    def test_contradictions_cited_across_clusters(self, conn):
        self._seed_story(conn)
        clusters = cluster_claims(conn, domain="economics")
        merged = next(c for c in clusters if c["size"] == 2)
        assert any(
            contradiction["claim_id"] == "c3"
            for contradiction in merged["contradictions"]
        )
        lone = next(c for c in clusters if c["size"] == 1)
        assert any(
            contradiction["claim_id"] in {"c1", "c2"}
            for contradiction in lone["contradictions"]
        )

    def test_representative_prefers_quality_and_recency(self, conn):
        self._seed_story(conn)
        # Give the older source a dominant transparency score.
        conn.execute(
            "INSERT INTO outlet_scores (source, source_type, score_date,"
            " composite_score) VALUES ('reuters', 'news', '2026-07-01', 0.99)"
        )
        conn.execute(
            "INSERT INTO outlet_scores (source, source_type, score_date,"
            " composite_score) VALUES ('bbc', 'news', '2026-07-01', 0.10)"
        )
        clusters = cluster_claims(conn, domain="economics")
        merged = next(c for c in clusters if c["size"] == 2)
        # 0.6*recency + 0.4*quality: reuters' 0.99 beats bbc's newer arrival.
        assert merged["representative"]["source"] == "reuters"

    def test_superseded_marked_and_not_representative(self, conn):
        _seed_claims(
            conn,
            [
                ("old", DUP_A, "d1", BASE_MS, "economics"),
                ("new", DUP_B, "d2", BASE_MS + 5 * DAY_MS, "economics"),
            ],
        )
        conn.execute(
            "UPDATE documents SET source_id = 'same-wire' "
            "WHERE document_id IN ('d1', 'd2')"
        )
        _link_and_cluster(conn)
        clusters = cluster_claims(conn, domain="economics")
        cluster = clusters[0]
        flags = {c["claim_id"]: c["superseded"] for c in cluster["citations"]}
        assert flags == {"new": False, "old": True}
        assert cluster["representative"]["claim_id"] == "new"

    def test_singletons_are_clusters_of_one(self, conn):
        _seed_claims(conn, [("solo", DUP_A, "d1", BASE_MS, "economics")])
        run_clustering_pass(conn)
        clusters = cluster_claims(conn, domain="economics")
        assert len(clusters) == 1
        assert clusters[0]["cluster_id"] == "cl-solo"
        assert clusters[0]["size"] == 1

    def test_since_filter_on_cluster_recency(self, conn):
        self._seed_story(conn)
        clusters = cluster_claims(
            conn, domain="economics", since=BASE_MS + DAY_MS
        )
        # Every cluster whose newest member arrived on day 2 qualifies —
        # including the merged one (its newest member is c2).
        assert {c["cluster_id"] for c in clusters} == {"cl-c1", "cl-c3"}

    def test_backing_claims_wires_through(self, conn):
        from src.kb import load_registry

        self._seed_story(conn)
        registry = load_registry()
        backing = registry.resolve("economics", conn=conn)
        clusters = backing.claims()
        assert any(c["size"] == 2 for c in clusters)
