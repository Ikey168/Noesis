"""M7.3: threshold calibration for the review-gated OSINT tools on a labeled
fixture. ``narrative_coordination``'s echo threshold is swept over coordinated
vs coincidental cohorts to measure its false-positive rate; ``geolocate_claims``
is measured to refuse every person location. This is the evidence behind
review-gate criterion 2 (a documented false-positive rate)."""

from src.osint.gated_calibration import (
    TARGET_FPR,
    calibrate_coordination,
    geolocate_person_refusal_rate,
)


# A labeled fixture. One coordinated cohort (four sources echoing an identical
# claim), one coincidental cohort (two sources whose claims merely overlap), and
# one distinct cohort (two unrelated claims). The coincidental pair is tuned to a
# Jaccard of 4/7 ~= 0.571 so it echoes at loose thresholds but not at 0.6.
_COORDINATED = {
    "coordinated": True,
    "cohort": [
        ("Alpha Wire", "The new policy will cut emissions forty percent by 2030 officials said."),
        ("Beta Journal", "The new policy will cut emissions forty percent by 2030 officials said."),
        ("Gamma Review", "The new policy will cut emissions forty percent by 2030 officials said."),
        ("Delta Times", "The new policy will cut emissions forty percent by 2030 officials said."),
    ],
}
_COINCIDENTAL = {
    "coordinated": False,
    "cohort": [
        ("Alpha Wire", "Severe flooding displaced thousands in the delta region"),
        ("Beta Journal", "Severe flooding displaced thousands of homes"),
    ],
}
_DISTINCT = {
    "coordinated": False,
    "cohort": [
        ("Alpha Wire", "The central bank raised interest rates again"),
        ("Beta Journal", "A new vaccine trial reported strong results"),
    ],
}
_FIXTURE = [_COORDINATED, _COINCIDENTAL, _DISTINCT]


def test_coordination_calibration_reports_fpr_tpr_per_threshold():
    report = calibrate_coordination(_FIXTURE)
    assert report["target_fpr"] == TARGET_FPR
    by_level = {r["min_similarity"]: r for r in report["levels"]}

    # The coordinated cohort (identical claims, Jaccard 1.0) is caught at every
    # threshold: true-positive rate is 1.0 throughout.
    for row in report["levels"]:
        assert row["tpr"] == 1.0

    # Loose thresholds flag the coincidental pair -> a real, measured false
    # positive; tight thresholds do not.
    assert by_level[0.3]["fpr"] > TARGET_FPR
    assert by_level[0.4]["fpr"] > TARGET_FPR
    assert by_level[0.6]["fpr"] == 0.0


def test_coordination_calibration_recommends_a_threshold_within_target_fpr():
    report = calibrate_coordination(_FIXTURE)
    rec = report["recommended"]
    # The recommendation is the smallest threshold whose false-positive rate is
    # within target while still catching coordination; it lands at the served
    # default (0.6) and its FPR is documented.
    assert rec["fpr"] <= TARGET_FPR
    assert rec["tpr"] >= 0.5
    assert rec["min_similarity"] == 0.6


def test_geolocate_person_false_positive_rate_is_zero():
    result = geolocate_person_refusal_rate(["Jordan Rivera", "Casey Morgan", "Lee Park"])
    assert result["people"] == 3
    assert result["refused"] == 3
    # The abusable failure mode -- emitting a person's location -- never fires.
    assert result["person_location_fpr"] == 0.0
