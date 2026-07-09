"""
Coverage tests for src/argument_mining/conflict_graph.py (#112).

Exercises the pure similarity/polarity helpers, the conflict-detection state
machine, and the DB-driven batch + graph builders. DB access is mocked with a
SQL-routing fake connection so these run offline.
"""
from __future__ import annotations

import threading
import unittest
from typing import List
from unittest.mock import MagicMock

from src.argument_mining.conflict_graph import (
    ClaimConflict,
    build_conflict_graph,
    compute_claim_conflicts,
    cosine_similarity,
    detect_conflict,
    _conflict_id,
    _sentiment_polarity,
    _tokenize,
)


# ---------------------------------------------------------------------------
# SQL-routing fake connection
# ---------------------------------------------------------------------------

class _RoutingConn:
    """DuckDB-alike whose fetchall/fetchone return preset rows keyed by a
    substring found in the executed SQL. Records every executed statement."""

    def __init__(self, routes=None):
        # routes: list of (substring, rows) tuples, first match wins
        self._routes = routes or []
        self.executed = []          # (sql, params)
        self.executemany_calls = []  # (sql, seq)
        self._lock = threading.Lock()

    def _rows_for(self, sql):
        for needle, rows in self._routes:
            if needle in sql:
                return rows
        return []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = self._rows_for(sql)
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
        return cur

    def executemany(self, sql, seq):
        self.executemany_calls.append((sql, list(seq)))
        return MagicMock()


def _lock():
    return threading.Lock()


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize(unittest.TestCase):
    def test_drops_stopwords_and_short_tokens(self):
        toks = _tokenize("The economy will grow and inflation is up")
        # "the", "will", "and" are stop-words; "is" and "up" < 3 chars removed
        self.assertIn("economy", toks)
        self.assertIn("grow", toks)
        self.assertIn("inflation", toks)
        self.assertNotIn("the", toks)
        self.assertNotIn("will", toks)
        self.assertNotIn("and", toks)

    def test_lowercases(self):
        self.assertEqual(_tokenize("ECONOMY Economy"), ["economy", "economy"])

    def test_empty_when_all_stopwords(self):
        self.assertEqual(_tokenize("the and for that this"), [])


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity(unittest.TestCase):
    def test_identical_text_is_one(self):
        s = cosine_similarity("economy inflation growth", "economy inflation growth")
        self.assertEqual(s, 1.0)

    def test_disjoint_vocab_is_zero(self):
        s = cosine_similarity("economy inflation", "football stadium")
        self.assertEqual(s, 0.0)

    def test_empty_tokens_returns_zero(self):
        # all stop-words -> empty token list on one side
        self.assertEqual(cosine_similarity("the and for", "economy inflation"), 0.0)
        self.assertEqual(cosine_similarity("", ""), 0.0)

    def test_partial_overlap_between_zero_and_one(self):
        s = cosine_similarity(
            "economy inflation growth prices",
            "economy inflation decline prices",
        )
        self.assertGreater(s, 0.0)
        self.assertLess(s, 1.0)

    def test_result_is_rounded_four_places(self):
        s = cosine_similarity("economy inflation growth", "economy prices growth")
        self.assertEqual(round(s, 4), s)


# ---------------------------------------------------------------------------
# _sentiment_polarity
# ---------------------------------------------------------------------------

class TestSentimentPolarity(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(_sentiment_polarity("The market will rise and improve"), 1)

    def test_negative(self):
        self.assertEqual(_sentiment_polarity("The market will fall and decline"), -1)

    def test_neutral_when_balanced(self):
        self.assertEqual(_sentiment_polarity("prices rise then fall"), 0)

    def test_neutral_when_no_keywords(self):
        self.assertEqual(_sentiment_polarity("committee met on tuesday"), 0)


# ---------------------------------------------------------------------------
# _conflict_id
# ---------------------------------------------------------------------------

class TestConflictId(unittest.TestCase):
    def test_canonical_order(self):
        self.assertEqual(_conflict_id("b", "a"), ("a", "b"))
        self.assertEqual(_conflict_id("a", "b"), ("a", "b"))

    def test_symmetric(self):
        self.assertEqual(_conflict_id("z9", "a1"), _conflict_id("a1", "z9"))


# ---------------------------------------------------------------------------
# detect_conflict — each branch of the state machine
# ---------------------------------------------------------------------------

class TestDetectConflict(unittest.TestCase):
    def test_direct_via_high_sim_and_opposite_polarity(self):
        # near-identical text, one opposing polarity word each -> sim>=0.80 branch
        c = detect_conflict(
            "a", "economy inflation prices growth markets outlook forecast rise",
            "news",
            "b", "economy inflation prices growth markets outlook forecast fall",
            "blog",
            topic="Economy",
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.conflict_type, "direct")
        self.assertGreaterEqual(c.similarity_score, 0.80)

    def test_direct_via_explicit_contradiction_mid_sim(self):
        # sim in [0.45, 0.80), explicit contradiction -> direct branch
        c = detect_conflict(
            "a", "economy inflation prices growth stable outlook",
            "news",
            "b", "economy inflation prices markets weak outlook",
            "news",
            topic="Economy",
            explicit_contradicts=True,
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.conflict_type, "direct")
        self.assertGreaterEqual(c.similarity_score, 0.45)

    def test_implied_via_cross_format(self):
        # moderate sim (>=0.65), no explicit, different source types -> implied
        c = detect_conflict(
            "a", "economy inflation prices growth markets stable",
            "news",
            "b", "economy inflation prices growth markets outlook",
            "paper",
            topic="Economy",
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.conflict_type, "implied")

    def test_implied_via_explicit_low_sim(self):
        # sim in [0.30, 0.45), explicit contradiction -> implied-explicit branch
        c = detect_conflict(
            "a", "economy inflation growth markets committee session policy",
            "news",
            "b", "economy inflation growth healthcare reform program funding",
            "news",
            topic="Economy",
            explicit_contradicts=True,
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.conflict_type, "implied")
        self.assertLess(c.similarity_score, 0.45)
        self.assertGreaterEqual(c.similarity_score, 0.30)

    def test_returns_none_when_no_signal(self):
        # high-ish similarity but same format, no polarity, no explicit -> None
        c = detect_conflict(
            "a", "economy inflation prices growth markets stable",
            "news",
            "b", "economy inflation prices growth markets stable",
            "news",
            topic="Economy",
            explicit_contradicts=False,
        )
        self.assertIsNone(c)

    def test_returns_none_when_low_sim_no_explicit(self):
        c = detect_conflict(
            "a", "football stadium crowd",
            "news",
            "b", "economy inflation prices",
            "blog",
            topic="Sports",
        )
        self.assertIsNone(c)

    def test_canonical_ids_and_source_types_ordered(self):
        # Pass ids out of order; result must be canonicalised, with source_type
        # tracking the id that ended up first.
        c = detect_conflict(
            "zzz", "economy inflation prices growth markets outlook forecast rise",
            "news",
            "aaa", "economy inflation prices growth markets outlook forecast fall",
            "blog",
            topic="Economy",
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.claim_id_a, "aaa")
        self.assertEqual(c.claim_id_b, "zzz")
        # "aaa" originally carried source_type "blog"
        self.assertEqual(c.source_type_a, "blog")
        self.assertEqual(c.source_type_b, "news")

    def test_returns_claimconflict_dataclass(self):
        c = detect_conflict(
            "a", "economy inflation prices growth markets outlook forecast rise",
            "news",
            "b", "economy inflation prices growth markets outlook forecast fall",
            "blog",
            topic="Economy",
        )
        self.assertIsInstance(c, ClaimConflict)
        self.assertEqual(c.topic, "Economy")
        self.assertTrue(c.computed_at)


# ---------------------------------------------------------------------------
# compute_claim_conflicts
# ---------------------------------------------------------------------------

class TestComputeClaimConflicts(unittest.TestCase):
    def test_no_claims_returns_zeros(self):
        conn = _RoutingConn(routes=[("FROM argument_claims", [])])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(
            out, {"claims_loaded": 0, "pairs_tested": 0, "conflicts_stored": 0}
        )

    def _claim_rows(self):
        # (claim_id, claim_text, source_type, topic, source_name, article_id)
        # sim ~0.875 + opposite polarity -> "direct" conflict
        return [
            ("c1", "economy inflation prices growth markets outlook forecast rise",
             "news", "Economy", "Reuters", 1),
            ("c2", "economy inflation prices growth markets outlook forecast fall",
             "blog", "Economy", "SomeBlog", 2),
        ]

    def test_detects_and_stores_conflict(self):
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", self._claim_rows()),   # claim load
            ("FROM claim_evidence e", []),                    # no explicit links
        ])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(out["claims_loaded"], 2)
        self.assertEqual(out["pairs_tested"], 1)
        self.assertEqual(out["conflicts_stored"], 1)
        # A store must have happened via executemany INSERT
        self.assertTrue(conn.executemany_calls)
        stored_sql, stored_rows = conn.executemany_calls[0]
        self.assertIn("claim_conflicts", stored_sql)
        self.assertEqual(len(stored_rows), 1)

    def test_same_source_pairs_skipped(self):
        # Both claims share the same source_name -> pair skipped, nothing tested
        rows = [
            ("c1", "economy inflation prices growth will rise improve",
             "news", "Economy", "Reuters", 1),
            ("c2", "economy inflation prices growth will fall decline",
             "blog", "Economy", "Reuters", 1),
        ]
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", rows),
            ("FROM claim_evidence e", []),
        ])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(out["pairs_tested"], 0)
        self.assertEqual(out["conflicts_stored"], 0)

    def test_explicit_contradiction_from_evidence(self):
        # No polarity/cross-format cue, but claim_evidence marks them contradicting.
        claim_rows = [
            ("c1", "economy inflation prices growth outlook markets stable regional",
             "news", "Economy", "Reuters", 1),
            ("c2", "economy healthcare policy reform program markets prices stable",
             "news", "Economy", "AP", 2),
        ]
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", claim_rows),
            ("FROM claim_evidence e", [("c1", "c2")]),  # explicit contradicts
        ])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(out["pairs_tested"], 1)
        self.assertEqual(out["conflicts_stored"], 1)

    def test_date_range_adds_filter_param(self):
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", self._claim_rows()),
            ("FROM claim_evidence e", []),
        ])
        compute_claim_conflicts(conn, _lock(), limit=50, date_range="2026-01-01")
        # The claim-load statement (found by content, since the corpus-table
        # resolver may run introspection queries first); params include date + limit
        load_sql, load_params = next(
            e for e in conn.executed if "FROM argument_claims c" in e[0]
        )
        self.assertIn("2026-01-01", load_params)
        self.assertIn("2026-01-01", load_params)
        self.assertIn(50, load_params)

    def test_max_pairs_per_topic_cap(self):
        # >200 pairwise-testable claims in one topic -> the MAX_PAIRS_PER_TOPIC
        # cap (200) stops the inner/outer loops early.
        claim_rows = [
            (f"c{i}", f"economy report number {i} committee",
             "news", "Economy", f"Outlet{i}", i)
            for i in range(60)  # 60 distinct sources -> 60*59/2 = 1770 possible pairs
        ]
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", claim_rows),
            ("FROM claim_evidence e", []),
        ])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(out["claims_loaded"], 60)
        # capped at MAX_PAIRS_PER_TOPIC (200), never the full 1770
        self.assertEqual(out["pairs_tested"], 200)

    def test_no_conflicts_means_no_store(self):
        # Unrelated topics/text -> no conflict, no executemany INSERT
        rows = [
            ("c1", "football stadium crowd match", "news", "Sports", "ESPN", 1),
            ("c2", "economy inflation prices growth", "blog", "Economy", "Blog", 2),
        ]
        conn = _RoutingConn(routes=[
            ("FROM argument_claims c", rows),
            ("FROM claim_evidence e", []),
        ])
        out = compute_claim_conflicts(conn, _lock())
        self.assertEqual(out["conflicts_stored"], 0)
        self.assertFalse(conn.executemany_calls)


# ---------------------------------------------------------------------------
# build_conflict_graph
# ---------------------------------------------------------------------------

class TestBuildConflictGraph(unittest.TestCase):
    def _graph_row(self):
        # Matches the 18-column SELECT in build_conflict_graph
        return (
            "c1", "claim A text", "news", 0.9, "2026-06-01", "Reuters", "Economy", 1,
            "c2", "claim B text", "blog", 0.8, "2026-06-02", "SomeBlog", "Economy", 2,
            0.83, "direct",
        )

    def test_builds_nodes_and_edges(self):
        conn = _RoutingConn(routes=[("FROM claim_conflicts cf", [self._graph_row()])])
        g = build_conflict_graph(conn, _lock())
        self.assertEqual(g["node_count"], 2)
        self.assertEqual(g["edge_count"], 1)
        self.assertEqual(g["_source"], "claim_conflicts")
        ids = {n["id"] for n in g["nodes"]}
        self.assertEqual(ids, {"c1", "c2"})
        edge = g["edges"][0]
        self.assertEqual(edge["source"], "c1")
        self.assertEqual(edge["target"], "c2")
        self.assertEqual(edge["severity"], 0.83)
        self.assertEqual(edge["relation"], "contradicts")

    def test_dedupes_shared_node_across_edges(self):
        # Two edges sharing c1 -> 3 unique nodes, 2 edges
        r1 = self._graph_row()
        r2 = (
            "c1", "claim A text", "news", 0.9, "2026-06-01", "Reuters", "Economy", 1,
            "c3", "claim C text", "paper", 0.7, "2026-06-03", "Journal", "Economy", 3,
            0.71, "implied",
        )
        conn = _RoutingConn(routes=[("FROM claim_conflicts cf", [r1, r2])])
        g = build_conflict_graph(conn, _lock())
        self.assertEqual(g["node_count"], 3)
        self.assertEqual(g["edge_count"], 2)

    def test_empty_when_no_rows(self):
        conn = _RoutingConn(routes=[("FROM claim_conflicts cf", [])])
        g = build_conflict_graph(conn, _lock())
        self.assertEqual(g["node_count"], 0)
        self.assertEqual(g["edge_count"], 0)
        self.assertEqual(g["nodes"], [])

    def test_filters_add_where_and_params(self):
        conn = _RoutingConn(routes=[("FROM claim_conflicts cf", [self._graph_row()])])
        build_conflict_graph(
            conn, _lock(),
            topic="Economy", source_type="news",
            date_cutoff="2026-05-01", limit=10,
        )
        sql, params = next(e for e in conn.executed if "claim_conflicts cf" in e[0])
        self.assertIn("WHERE", sql)
        self.assertIn("news", params)          # source_type filter (twice)
        self.assertIn("%Economy%", params)     # topic ILIKE
        self.assertIn("2026-05-01", params)    # date cutoff
        self.assertIn(10, params)              # limit is last

    def test_none_confidence_defaults(self):
        row = (
            "c1", "A", "news", None, None, "Reuters", "Economy", 1,
            "c2", "B", "blog", None, None, "Blog", "Economy", 2,
            0.9, "direct",
        )
        conn = _RoutingConn(routes=[("FROM claim_conflicts cf", [row])])
        g = build_conflict_graph(conn, _lock())
        node = g["nodes"][0]
        self.assertEqual(node["confidence"], 0.5)
        self.assertIsNone(node["date"])

    def test_query_exception_yields_empty_graph(self):
        # Connection whose execute raises -> the try/except sets rows = []
        class _BoomConn:
            _lock = threading.Lock()

            def execute(self, *a, **k):
                raise RuntimeError("boom")

        g = build_conflict_graph(_BoomConn(), _lock())
        self.assertEqual(g["node_count"], 0)
        self.assertEqual(g["edge_count"], 0)


if __name__ == "__main__":
    unittest.main()
