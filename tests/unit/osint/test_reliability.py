"""Tests for source_reliability() (R10 #612)."""

import math

from src.analytics.honesty import validate_analytic_output
from src.osint import source_reliability


def test_reliability_renders_for_a_non_news_source(seed):
    # A blog venue, scored the same way an outlet is (outlet_scores keys on
    # source, not on being news).
    seed.articles(
        [
            ("b1", "post one", "http://x/1", "Indie Energy Blog", "2026-06-01"),
            ("b2", "post two", "http://x/2", "Indie Energy Blog", "2026-06-02"),
            ("b3", "post three", "http://x/3", "Indie Energy Blog", "2026-06-03"),
        ]
    )
    seed.claims(
        [
            ("c1", "Solar beats gas on cost.", "b1", "blog", 0.9, "supported"),
            ("c2", "Grid can absorb 80 percent renewables.", "b2", "blog", 0.7, "unverified"),
            ("c3", "A later-disputed claim.", "b3", "blog", 0.6, "disputed"),
        ]
    )
    seed.evidence([("e1", "c1", "z1", "news", "supports", 0.8)])
    seed.outlet_scores(
        [("Indie Energy Blog", "blog", "2026-06-03", 0.7, 0.8, 0.7, 0.75)]
    )

    out = source_reliability(seed.conn, "Indie Energy Blog")
    assert validate_analytic_output(out, interval_fields=("reliability",)) == []
    assert out["found"] is True
    assert out["scored_as_outlet"] is True
    assert out["track_record"]["documents"] == 3
    assert out["corroboration"]["corroborated_claims"] == 1  # only c1 has support
    assert out["corrections"]["disputed_claims"] == 1  # c3
    assert 0.0 <= out["reliability"]["value"] <= 1.0


def test_sparse_source_has_a_wider_interval(seed):
    seed.articles([("a1", "one doc", "http://y/1", "Thin Source", "2026-06-01")])
    seed.claims([("c1", "A lone claim.", "a1", "news", 0.5, None)])
    thin = source_reliability(seed.conn, "Thin Source")

    seed.articles(
        [(f"m{i}", "doc", f"http://z/{i}", "Rich Source", "2026-06-01") for i in range(20)]
    )
    rich = source_reliability(seed.conn, "Rich Source")

    thin_width = thin["reliability"]["hi"] - thin["reliability"]["lo"]
    rich_width = rich["reliability"]["hi"] - rich["reliability"]["lo"]
    assert thin_width > rich_width


def test_unknown_source_is_not_found_but_still_valid(seed):
    seed.articles([("a1", "doc", "http://y/1", "Known", "2026-06-01")])
    out = source_reliability(seed.conn, "Never Heard Of It")
    assert out["found"] is False
    assert validate_analytic_output(out, interval_fields=("reliability",)) == []
