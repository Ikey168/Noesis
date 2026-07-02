"""Unit tests for graph analytics (src/analytics/graph.py)."""

from src.analytics.graph import degree, label_propagation, pagerank


def test_pagerank_ranks_hub_highest():
    # A star: 'hub' connects to 4 leaves.
    nodes = ["hub", "a", "b", "c", "d"]
    edges = [("hub", "a"), ("hub", "b"), ("hub", "c"), ("hub", "d")]
    rank = pagerank(nodes, edges)
    assert abs(sum(rank.values()) - 1.0) < 1e-6
    assert rank["hub"] == max(rank.values())
    assert rank["hub"] > rank["a"]


def test_pagerank_empty_graph():
    assert pagerank([], []) == {}


def test_pagerank_isolated_nodes_share_mass():
    rank = pagerank(["x", "y"], [])
    assert rank["x"] == rank["y"]


def test_label_propagation_finds_two_communities():
    # Two triangles joined by nothing.
    nodes = ["a", "b", "c", "x", "y", "z"]
    edges = [("a", "b"), ("b", "c"), ("a", "c"), ("x", "y"), ("y", "z"), ("x", "z")]
    comm = label_propagation(nodes, edges)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]
    # Communities renumbered from 0.
    assert set(comm.values()) == {0, 1}


def test_label_propagation_is_deterministic():
    nodes = ["a", "b", "c", "d"]
    edges = [("a", "b"), ("c", "d")]
    assert label_propagation(nodes, edges) == label_propagation(nodes, edges)


def test_label_propagation_empty():
    assert label_propagation([], []) == {}


def test_degree_counts_and_ignores_self_loops():
    deg = degree(["a", "b", "c"], [("a", "b"), ("a", "c"), ("a", "a")])
    assert deg == {"a": 2, "b": 1, "c": 1}


def test_edges_to_unknown_nodes_dropped():
    rank = pagerank(["a", "b"], [("a", "b"), ("a", "ghost")])
    assert set(rank) == {"a", "b"}
