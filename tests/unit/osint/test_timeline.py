"""Tests for timeline_reconstruct() (R11 #617)."""

from src.osint import timeline_reconstruct


def _corpus(seed):
    seed.articles(
        [
            ("d1", "rule proposed", "http://a/1", "Alpha Wire", "2025-11-03"),
            ("d2", "delay confirmed A", "http://b/1", "Beta Journal", "2026-02-14"),
            ("d3", "delay confirmed B", "http://c/1", "Gamma Review", "2026-02-14"),
            ("d4", "anon reversal", None, "unknown", "2026-06-21"),
        ]
    )
    seed.claims(
        [
            ("k1", "The emissions rule was proposed.", "d1", "news", 0.9, None),
            ("k2", "The rule is delayed.", "d2", "news", 0.8, None),
            ("k3", "The rule is delayed.", "d3", "news", 0.8, None),
            ("k4", "The rule is reversed.", "missing_doc", "news", 0.4, None),
        ]
    )


def test_events_carry_corroboration_density(seed):
    _corpus(seed)
    out = timeline_reconstruct(seed.conn, topic="rule")
    by_date = {e["date"]: e for e in out["events"]}
    # Two independent sources on 2026-02-14 -> density 2, state cited.
    assert by_date["2026-02-14"]["corroboration_density"] == 2
    assert by_date["2026-02-14"]["state"] == "cited"
    # First event single-sourced.
    assert by_date["2025-11-03"]["state"] == "single_sourced"


def test_uncited_entry_is_flagged(seed):
    _corpus(seed)
    out = timeline_reconstruct(seed.conn, topic="rule")
    undated = [e for e in out["events"] if e["date"] == "undated"]
    assert undated and undated[0]["uncited_count"] == 1  # k4 has no resolvable doc
    assert undated[0]["state"] == "uncited"


def test_entity_scoped_timeline(seed):
    _corpus(seed)
    seed.actors([("d1", "Grid Authority", "org:ga", "subject")])
    out = timeline_reconstruct(seed.conn, entity="Grid Authority")
    assert out["count"] == 1  # only d1's claim
    assert out["events"][0]["entries"][0]["claim_id"] == "k1"


def test_unknown_entity_is_graceful(seed):
    _corpus(seed)
    seed.actors([])
    out = timeline_reconstruct(seed.conn, entity="Nobody")
    assert out["count"] == 0
