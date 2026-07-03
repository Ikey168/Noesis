"""Tests for contradiction_scan() (R10 #613)."""

from src.osint import contradiction_scan


def _base(seed):
    seed.articles(
        [
            ("d1", "cost fell", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "cost flat", "http://d/1", "Delta Post", "2026-06-01"),
            ("d3", "rule effective", "http://f/1", "Federal Register", "2026-06-01"),
        ]
    )
    seed.claims(
        [
            ("k1", "Storage costs fell 40 percent.", "d1", "news", 0.9, None),
            ("k2", "Storage costs are flat.", "d2", "news", 0.8, None),
            ("k3", "The rule takes effect in 90 days.", "d3", "news", 0.9, None),
            # k4 has no document -> uncited, must be flagged not hidden.
            ("k4", "The rule is delayed indefinitely.", "missing_doc", "news", 0.5, None),
        ]
    )


def test_scan_renders_cited_contradiction_ledger(seed):
    _base(seed)
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "energy storage")])
    out = contradiction_scan(seed.conn, topic="storage")
    assert out["count"] == 1
    pair = out["contradictions"][0]
    assert pair["cited"] is True
    assert {pair["claim_a"]["source"], pair["claim_b"]["source"]} == {"Alpha Wire", "Delta Post"}
    assert pair["claim_a"]["url"] and pair["claim_b"]["url"]


def test_uncited_entry_is_flagged_not_hidden(seed):
    _base(seed)
    seed.conflicts([("k3", "k4", "contradicts", 0.7, "emissions rule")])
    out = contradiction_scan(seed.conn, topic="emissions")
    assert out["count"] == 1  # not dropped
    assert out["uncited_count"] == 1
    pair = out["contradictions"][0]
    assert pair["cited"] is False
    # The uncited claim resolves to no document.
    uncited = pair["claim_b"] if not pair["claim_b"]["cited"] else pair["claim_a"]
    assert uncited["document_id"] in (None, "missing_doc") and uncited["url"] is None


def test_topic_filter_scopes_results(seed):
    _base(seed)
    seed.conflicts(
        [
            ("k1", "k2", "contradicts", 0.8, "energy storage"),
            ("k3", "k4", "contradicts", 0.7, "emissions rule"),
        ]
    )
    assert contradiction_scan(seed.conn, topic="storage")["count"] == 1
    assert contradiction_scan(seed.conn)["count"] == 2  # unscoped: both


def test_entity_filter_matches_claim_text(seed):
    _base(seed)
    seed.conflicts(
        [
            ("k1", "k2", "contradicts", 0.8, "energy storage"),
            ("k3", "k4", "contradicts", 0.7, "emissions rule"),
        ]
    )
    out = contradiction_scan(seed.conn, entity="rule")
    assert out["count"] == 1
    assert "rule" in (out["contradictions"][0]["claim_a"]["text"] + out["contradictions"][0]["claim_b"]["text"]).lower()


def test_no_conflicts_table_is_graceful(seed):
    seed.claims([("k1", "x", "d1", "news", 0.5, None)])
    out = contradiction_scan(seed.conn, topic="anything")
    assert out["count"] == 0 and out["contradictions"] == []
