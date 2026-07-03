"""Tests for the review-gated OSINT tools (issue #639 item 3): purpose
limitation and defensive framing, exercised directly (the module is importable
regardless of the serving flag)."""

from src.osint import geolocate_claims, narrative_coordination


def _corpus(seed):
    seed.articles(
        [
            ("d1", "Flooding hits Paris and parts of France", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Talks continue in Berlin", "http://b/1", "Beta Journal", "2026-06-02"),
            ("d3", "Rivera speaks at the summit", "http://c/1", "Gamma Review", "2026-06-03"),
        ]
    )
    seed.claims(
        [
            ("k1", "Severe flooding struck Paris in June.", "d1", "news", 0.9, None),
            ("k2", "Delegates met in Berlin over the treaty.", "d2", "news", 0.8, None),
            ("k3", "Jordan Rivera addressed the summit.", "d3", "news", 0.7, None),
        ]
    )
    seed.actors([("d3", "Jordan Rivera", "person:jr", "speaker")])


# --- geolocate_claims: event geography only ---------------------------------


def test_geolocate_returns_event_geography_cited(seed):
    _corpus(seed)
    out = geolocate_claims(seed.conn, topic="flooding")
    places = {loc["location"] for loc in out["locations"]}
    assert "paris" in places or "france" in places
    for loc in out["locations"]:
        assert loc["kind"] == "event-geography"
        assert loc["verified"] is False  # never asserted as fact
        assert loc["cited"] is True


def test_geolocate_refuses_a_person(seed):
    _corpus(seed)
    out = geolocate_claims(seed.conn, entity="Jordan Rivera")
    assert out.get("code") == "person_geolocation_refused"
    assert "locations" not in out  # emits nothing for a person


def test_geolocate_never_labels_a_person_location(seed):
    _corpus(seed)
    out = geolocate_claims(seed.conn)  # unscoped
    # Every location is tied to a claim/event, never to a person entity.
    for loc in out["locations"]:
        assert "person" not in loc and "entity" not in loc
        assert "claim_id" in loc


# --- narrative_coordination: flag cohorts for review, never accuse ----------


def test_coordination_flags_echoing_sources_as_review(seed):
    # Three different sources publish a near-identical claim -> a cohort.
    seed.articles(
        [
            ("e1", "t", "http://x/1", "Source A", "2026-06-01"),
            ("e2", "t", "http://x/2", "Source B", "2026-06-01"),
            ("e3", "t", "http://x/3", "Source C", "2026-06-01"),
            ("e4", "t", "http://x/4", "Source D", "2026-06-01"),
        ]
    )
    echo = "The new policy will cut emissions by forty percent by 2030 officials said."
    seed.claims(
        [
            ("c1", echo, "e1", "news", 0.9, None),
            ("c2", echo, "e2", "news", 0.9, None),
            ("c3", echo, "e3", "news", 0.9, None),
            ("c4", "An entirely unrelated statement about sports.", "e4", "news", 0.5, None),
        ]
    )
    out = narrative_coordination(seed.conn)
    assert out["count"] >= 1
    cohort = out["cohorts"][0]
    assert set(cohort["sources"]) >= {"Source A", "Source B", "Source C"}
    assert "Source D" not in cohort["sources"]  # the unrelated source is not flagged
    # Never an accusation: status and caveat are review-framed.
    assert cohort["status"] == "warrants review"
    assert "not an accusation" in cohort["note"] and "coincidental" in cohort["note"]
    assert "caveat" in out and "review" in out["caveat"].lower()


def test_coordination_no_false_cohort_from_distinct_claims(seed):
    seed.articles(
        [
            ("e1", "t", "http://x/1", "Source A", "2026-06-01"),
            ("e2", "t", "http://x/2", "Source B", "2026-06-01"),
        ]
    )
    seed.claims(
        [
            ("c1", "Solar capacity rose sharply this quarter.", "e1", "news", 0.9, None),
            ("c2", "Interest rates were left unchanged by the bank.", "e2", "news", 0.9, None),
        ]
    )
    out = narrative_coordination(seed.conn)
    assert out["count"] == 0  # distinct claims -> no cohort flagged
