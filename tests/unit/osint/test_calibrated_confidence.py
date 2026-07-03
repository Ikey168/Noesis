"""M7.2: calibrated intervals in OSINT outputs. Corroboration strength and source
reliability carry a conformal range with a documented coverage rate, not a bare
number or an asserted band."""

from src.osint import corroborate, source_reliability
from src.analytics.honesty import is_interval


def test_reliability_interval_is_calibrated_with_documented_coverage(seed):
    seed.articles([("d1", "t", "http://a/1", "Alpha Wire", "2026-05-01")])
    seed.claims([("k1", "a claim", "d1", "news", 0.9, None)])
    seed.outlet_scores([("Alpha Wire", "outlet", "2026-05-01", 0.6, 0.7, 0.5, 0.62)])

    out = source_reliability(seed.conn, "Alpha Wire")
    assert is_interval(out["reliability"])
    assert out["reliability"]["level"] == 0.9
    # A measured coverage rate accompanies the interval (documented, in [0,1]),
    # not an asserted level: it is the fraction of components the band covers.
    assert 0.0 <= out["coverage"] <= 1.0
    assert out["calibration_n"] >= 2  # the components it measured coverage over


def test_corroboration_strength_carries_a_calibrated_range(seed):
    seed.articles([
        ("d1", "claim doc", "http://a/1", "Alpha Wire", "2026-06-01"),
        ("d2", "support one", "http://b/1", "Beta Journal", "2026-06-02"),
        ("d3", "support two", "http://c/1", "Gamma Review", "2026-06-03"),
    ])
    seed.claims([("k1", "Severe flooding struck the delta.", "d1", "news", 0.9, None)])
    seed.evidence([
        ("e1", "k1", "d2", "news", "supports", 0.88),
        ("e2", "k1", "d3", "news", "supports", 0.82),
    ])
    seed.outlet_scores([
        ("Beta Journal", "outlet", "2026-06-01", 0.6, 0.8, 0.6, 0.8),
        ("Gamma Review", "outlet", "2026-06-01", 0.5, 0.6, 0.5, 0.6),
    ])

    out = corroborate(seed.conn, "k1")
    assert out["independent_support_count"] == 2
    assert is_interval(out["support_credibility"])
    assert out["support_credibility"]["level"] == 0.9
    assert out["support_coverage"] >= 0.9
    assert out["support_calibration_n"] == 2


def test_single_sourced_claim_has_no_calibrated_range(seed):
    seed.articles([("d1", "lonely claim", "http://a/1", "Alpha Wire", "2026-06-01")])
    seed.claims([("k1", "An uncorroborated claim.", "d1", "news", 0.7, None)])

    out = corroborate(seed.conn, "k1")
    assert out["single_sourced"] is True
    assert out["support_credibility"] is None
    assert out["support_coverage"] is None
