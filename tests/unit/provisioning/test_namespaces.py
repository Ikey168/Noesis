"""Namespacing + routing tests (R8 #606).

The exit criterion: a namespaced KG holds only routed documents; shared tables
are untouched; the namespace is visible in the registry/lineage.
"""

from datetime import datetime, timezone

import pytest

from src.provisioning import namespaces


def _now():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_name_validation_rejects_injection_and_shapes():
    for bad in ["Bad", "1kg", "kg-name", "kg name", "kg;drop", "", "x" * 40, None]:
        assert not namespaces.valid_name(bad)
    for good in ["climate", "kg2", "semi_conductors", "a1"]:
        assert namespaces.valid_name(good)


def test_namespace_tables_are_prefixed():
    tables = namespaces.namespace_tables("climate")
    assert tables == {
        "documents": "kg_climate_documents",
        "entities": "kg_climate_entities",
        "claims": "kg_climate_claims",
    }


def test_require_valid_name_raises():
    with pytest.raises(ValueError):
        namespaces.namespace_prefix("Bad Name")


def _seed(seed):
    seed.articles(
        [
            ("a1", "Solar power sets new record", "u1", "c", _now(), "Alpha", "energy"),
            ("a2", "Grid storage limits growth", "u2", "c", _now(), "Alpha", "energy"),
            ("a3", "Chip fabs expand capacity", "u3", "c", _now(), "Beta", "tech"),
            ("a4", "Unrelated sports result", "u4", "c", _now(), "Gamma", "sport"),
        ]
    )
    seed.claims(
        [
            ("c1", "Solar is cheapest.", "a1", "news", 0.9, "supported"),
            ("c2", "Storage is the constraint.", "a2", "news", 0.8, "disputed"),
            ("c3", "Sports claim.", "a4", "news", 0.7, None),
        ]
    )


def test_routing_holds_only_routed_documents(seed):
    _seed(seed)
    conn = seed.conn
    # Route only Alpha's documents into the KG namespace.
    counts = namespaces.route_documents(conn, "energy", ["Alpha"], _now())
    assert counts["documents"] == 2
    assert counts["claims"] == 2  # only a1/a2 claims, not the sports claim

    ns = namespaces.namespace_tables("energy")
    routed_ids = {r[0] for r in conn.execute(f"SELECT id FROM {ns['documents']}").fetchall()}
    assert routed_ids == {"a1", "a2"}
    # The Beta/Gamma documents never entered the namespace.
    assert "a3" not in routed_ids and "a4" not in routed_ids


def test_routing_leaves_shared_tables_untouched(seed):
    _seed(seed)
    conn = seed.conn
    before = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    before_claims = conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()[0]
    namespaces.route_documents(conn, "energy", ["Alpha"], _now())
    after = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    after_claims = conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()[0]
    assert (before, before_claims) == (after, after_claims) == (4, 3)


def test_routing_is_idempotent(seed):
    _seed(seed)
    conn = seed.conn
    first = namespaces.route_documents(conn, "energy", ["Alpha"], _now())
    second = namespaces.route_documents(conn, "energy", ["Alpha"], _now())
    assert first["documents"] == 2
    assert second["documents"] == 0  # nothing new on re-run
    ns = namespaces.namespace_tables("energy")
    total = conn.execute(f"SELECT COUNT(*) FROM {ns['documents']}").fetchone()[0]
    assert total == 2  # no duplication


def test_archive_renames_without_deleting(seed):
    _seed(seed)
    conn = seed.conn
    namespaces.route_documents(conn, "energy", ["Alpha"], _now())
    archived = namespaces.archive_namespace(conn, "energy")
    # Live tables are gone, archived copies remain with the data.
    assert namespaces.namespace_counts(conn, "energy")["documents"] == 0
    kept = conn.execute(f"SELECT COUNT(*) FROM {archived['documents']}").fetchone()[0]
    assert kept == 2
