"""Tests for corroborate() (R10 #611)."""

from src.analytics.honesty import validate_analytic_output
from src.osint import corroborate


def _base(seed):
    seed.articles(
        [
            ("d1", "Alpha claim doc", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Beta support doc", "http://b/1", "Beta Journal", "2026-06-01"),
            ("d3", "Gamma support doc", "http://g/1", "Gamma Review", "2026-06-01"),
            ("d4", "Delta counter doc", "http://d/1", "Delta Post", "2026-06-01"),
        ]
    )
    seed.claims(
        [
            ("k1", "Emissions rule cuts output 12 percent.", "d1", "news", 0.9, "unverified"),
            ("k2", "Emissions rule has no measurable effect.", "d4", "news", 0.8, "disputed"),
            ("lonely", "A claim nobody else touches.", "d1", "news", 0.5, None),
        ]
    )
    seed.outlet_scores(
        [
            ("Alpha Wire", "news", "2026-06-01", 0.7, 0.8, 0.7, 0.78),
            ("Beta Journal", "news", "2026-06-01", 0.8, 0.85, 0.75, 0.81),
            ("Gamma Review", "news", "2026-06-01", 0.6, 0.6, 0.6, 0.66),
            ("Delta Post", "news", "2026-06-01", 0.4, 0.4, 0.5, 0.42),
        ]
    )


def test_counts_independent_sources_for_and_against(seed):
    _base(seed)
    # Two independent sources support k1 via evidence; Delta contradicts via a
    # conflict edge (k2 from Delta Post contradicts k1).
    seed.evidence(
        [
            ("e1", "k1", "d2", "news", "supports", 0.9),
            ("e2", "k1", "d3", "news", "supports", 0.7),
        ]
    )
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "emissions")])

    out = corroborate(seed.conn, "k1")
    assert validate_analytic_output(out) == []
    assert out["independent_support_count"] == 2
    assert out["independent_contradict_count"] == 1
    assert {s["source"] for s in out["support"]} == {"Beta Journal", "Gamma Review"}
    assert out["contradict"][0]["source"] == "Delta Post"
    assert out["single_sourced"] is False
    # No single confidence number: weighted tallies, not one score.
    assert out["weighted_support"] > out["weighted_contradict"]
    assert "confidence" not in out


def test_single_sourced_claim_is_flagged(seed):
    _base(seed)
    out = corroborate(seed.conn, "lonely")
    assert out["single_sourced"] is True
    assert out["independent_support_count"] == 0
    assert out["independent_contradict_count"] == 0
    assert out["n"] == 0


def test_same_source_does_not_self_corroborate(seed):
    _base(seed)
    # Evidence pointing back at the claim's OWN source (Alpha Wire) must not
    # count as independent support.
    seed.evidence([("e1", "k1", "d1", "news", "supports", 0.95)])
    out = corroborate(seed.conn, "k1")
    assert out["independent_support_count"] == 0
    assert out["single_sourced"] is True


def test_credibility_weighting_uses_outlet_scores(seed):
    _base(seed)
    seed.evidence([("e1", "k1", "d2", "news", "supports", 0.9)])
    out = corroborate(seed.conn, "k1")
    support = out["support"][0]
    assert support["source"] == "Beta Journal"
    assert abs(support["credibility"] - 0.81) < 1e-6


def test_unknown_claim_errors(seed):
    _base(seed)
    assert "error" in corroborate(seed.conn, "nope")


# --- OX01 (#2041): Admiralty information-credibility grade -------------------


def test_well_corroborated_claim_grades_1_or_2(seed):
    _base(seed)
    seed.evidence(
        [
            ("e1", "k1", "d2", "news", "supports", 0.9),
            ("e2", "k1", "d3", "news", "supports", 0.7),
        ]
    )
    out = corroborate(seed.conn, "k1")
    assert out["credibility_grade"]["grade"] in (1, 2)
    assert out["credibility_grade"]["grade"] == 1
    assert out["grading"]["independent_axes"] is True
    assert "independent" in out["grading"]["note"]
    # Source reliability of the carrying source is reported beside it, separately.
    assert out["source_reliability_grade"]["axis"] == "source_reliability"
    assert out["grading"]["source_reliability"] == out["source_reliability_grade"]["grade"]


def test_single_sourced_claim_grades_5_or_6(seed):
    _base(seed)
    lonely = corroborate(seed.conn, "lonely")
    assert lonely["credibility_grade"]["grade"] == 6
    # Single-sourced and fact-checked as disputed reads as improbable.
    disputed = corroborate(seed.conn, "k2")
    assert disputed["single_sourced"] is True
    assert disputed["credibility_grade"]["grade"] == 5


def test_contested_claim_grades_by_weighted_balance(seed):
    _base(seed)
    seed.evidence([("e1", "k1", "d2", "news", "supports", 0.9)])
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "emissions")])
    out = corroborate(seed.conn, "k1")
    # Beta (0.81) supports, Delta (0.42) contradicts -> support < 2x -> 3.
    assert out["credibility_grade"]["grade"] == 3
    assert out["credibility_grade"]["derivation"]["inputs"]["probable_origin_contradict_count"] == 1


def test_only_contradicted_claim_grades_5(seed):
    _base(seed)
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "emissions")])
    assert corroborate(seed.conn, "k1")["credibility_grade"]["grade"] == 5


def test_unresolved_lineage_grades_6():
    from src.osint.corroboration import credibility_grade

    g = credibility_grade(
        support_origins=0, contradict_origins=0, weighted_support=0.5,
        weighted_contradict=0.0, single_sourced=False, unresolved=2,
    )
    assert g["grade"] == 6
    assert g["derivation"]["inputs"]["lineage_unresolved"] is True


def test_no_fused_score_field(seed):
    _base(seed)
    seed.evidence([("e1", "k1", "d2", "news", "supports", 0.9)])
    out = corroborate(seed.conn, "k1")
    fused = {"confidence", "fused_confidence", "overall_confidence", "admiralty_code", "rating"}
    assert not fused & set(out)
    assert not fused & set(out["grading"])
    assert set(out["grading"]) == {
        "source_reliability", "information_credibility", "independent_axes", "note"
    }
