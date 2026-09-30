"""Movement record model: schema validation, window bounds and coverage semantics (#2237, MV02)."""

from __future__ import annotations

import duckdb
import pytest

from src.osint.movements import (
    NO_COVERAGE,
    MovementError,
    MovementStore,
    classify,
    coverage_gaps,
    statement,
    thin,
    validate_statement,
    window_bound,
    window_id,
)

SOURCE = {"url": "https://opensky-network.org/api/tracks/all?icao24=a0f1b2", "locator": "/path/0",
          "evidence_origin": "fixture"}
SUBJECT = "aircraft:icao24:a0f1b2"


def window(start="2099-05-01T08:00:00Z", end="2099-05-01T12:00:00Z"):
    return {"window_id": window_id("opensky", SUBJECT, start, end, {"icao24": "a0f1b2"}), "start": start, "end": end}


def sample_window(samples=2, status="positions_observed", start="2099-05-01T08:00:00Z", end="2099-05-01T12:00:00Z",
                  **coverage):
    return statement(
        "sample_window", "opensky", SUBJECT, "window", {"query": {"icao24": "a0f1b2"}},
        identifiers=[{"scheme": "icao24", "value": "a0f1b2"}], source=SOURCE, event="observed",
        effective_from=start, effective_to=end, window=window(start, end), bound=window_bound("opensky"),
        coverage={"status": status, "samples_published": samples, "samples_stored": samples, "gaps": [],
                  "caveat": "receiver coverage", **coverage})


def sample(ts="2099-05-01T09:00:00Z", lat=51.47, lon=-0.45, win=None):
    return statement("position_sample", "opensky", SUBJECT, f"sample:{ts}",
                     {"timestamp": ts, "lat": lat, "lon": lon, "altitude_m": 1200.0},
                     source=SOURCE, event="observed", effective_from=ts, window=win or window())


def test_valid_window_and_sample_statements():
    assert sample_window()["coverage"]["status"] == "positions_observed"
    assert sample()["window"]["window_id"].startswith("mv-window:")


def test_a_sample_without_a_window_is_rejected():
    value = sample()
    value.pop("window")
    with pytest.raises(MovementError, match="window"):
        validate_statement(value)


def test_a_sample_outside_its_window_or_off_the_globe_is_rejected():
    with pytest.raises(MovementError, match="outside its window"):
        sample(ts="2099-05-02T09:00:00Z")
    with pytest.raises(MovementError, match="WGS84"):
        sample(lat=123.0)


def test_window_bounds_are_enforced():
    with pytest.raises(MovementError) as over:
        sample_window(end="2099-05-04T12:00:00Z")  # 76 h > the 48 h OpenSky bound
    assert over.value.code == "over_bound"
    with pytest.raises(MovementError, match="more samples"):
        sample_window(samples=501)


def test_coverage_is_explicit_and_never_no_movement():
    empty = sample_window(samples=0, status="no_coverage_observed")
    assert empty["coverage"]["status"] == "no_coverage_observed" and NO_COVERAGE == "no coverage observed"
    with pytest.raises(MovementError, match="no_coverage_observed"):
        sample_window(samples=0, status="positions_observed")
    with pytest.raises(MovementError, match="no_coverage_observed"):
        sample_window(samples=3, status="no_coverage_observed")
    value = sample_window()
    value["as_published"]["did_not_move"] = True
    with pytest.raises(MovementError, match="absence verdict"):
        validate_statement(value)


def test_gaps_are_declared_intervals_without_positions():
    gaps = coverage_gaps(["2099-05-01T08:05:00Z", "2099-05-01T08:10:00Z", "2099-05-01T11:00:00Z"],
                         "2099-05-01T08:00:00Z", "2099-05-01T12:00:00Z", 900)
    assert [(g["from"], g["to"], g["kind"]) for g in gaps] == [
        ("2099-05-01T08:10:00Z", "2099-05-01T11:00:00Z", "no positions received"),
        ("2099-05-01T11:00:00Z", "2099-05-01T12:00:00Z", "window edge without positions")]
    (whole,) = coverage_gaps([], "2099-05-01T08:00:00Z", "2099-05-01T09:00:00Z", 900)
    assert whole["seconds"] == 3600 and whole["kind"] == "no positions received"
    kept, thinned = thin(list(range(10)), 4)
    assert thinned and kept == [0, 3, 6, 9]


def test_identifiers_are_typed_and_person_keys_are_refused():
    assert classify("IMO 9000027") == ("imo", "9000027")
    assert classify("671000002") == ("mmsi", "671000002")
    assert classify("A0F1B2") == ("icao24", "a0f1b2")
    assert classify("N901EX") == ("registration", "N901EX")
    assert classify("G-EXMP") == ("registration", "G-EXMP")
    for person in ("Jane Example", "jane@example.org", "person:42"):
        with pytest.raises(MovementError) as refused:
            classify(person)
        assert refused.value.code == "person_identifier_refused"
    with pytest.raises(MovementError) as named:
        classify("anything", scheme="owner_name")
    assert named.value.code == "person_identifier_refused"
    with pytest.raises(MovementError, match="well-formed"):
        classify("9000028", scheme="imo")  # check digit fails
    bad = sample_window()
    bad["identifiers"] = [{"scheme": "pilot", "value": "Jane Example"}]
    with pytest.raises(MovementError):
        validate_statement(bad)


def test_registry_revisions_are_kept_and_replays_add_nothing():
    conn = duckdb.connect(":memory:")
    store = MovementStore(conn, now=lambda: 1)
    base = {"registration": "N901EX", "icao24": "a0f1b2", "status": "Valid",
            "registrant": {"kind": "organisation", "name": "EXAMPLE AIR LLC", "natural_person": False}}
    ids = [{"scheme": "registration", "value": "N901EX"}, {"scheme": "icao24", "value": "a0f1b2"}]

    def reg(published, event="registered"):
        return statement("registry_record", "faa-registry", "aircraft:registration:N901EX", "faa:N901EX", published,
                         identifiers=ids, source={"url": None, "locator": "/", "evidence_origin": "fixture"},
                         event=event, effective_from="2099-01-01")

    first = store.apply("osint", reg(base))
    assert store.apply("osint", reg(base))["status"] == "unchanged"
    second = store.apply("osint", reg({**base, "status": "Deregistered"}, "deregistered"))
    history = store.revisions("osint", first["record_id"])
    assert [r["event"] for r in history] == ["registered", "deregistered"]
    assert history[1]["supersedes"] == first["revision_id"] and second["status"] == "revised"
    assert store.subjects_for("osint", "icao24", "a0f1b2") == ["aircraft:registration:N901EX"]
