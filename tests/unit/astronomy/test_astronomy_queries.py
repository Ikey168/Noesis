"""As-of astronomy answers across revision boundaries (#2149, AS08)."""

from __future__ import annotations

import pytest

from src.kb.astronomy_queries import AstronomyQueries
from src.kb.astronomy_records import AstronomyError
from tests.unit.astronomy import harness as h


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    return conn


def q(conn):
    return AstronomyQueries(conn)


def test_every_answer_states_its_cutoff_and_not_ready_comes_first():
    with pytest.raises(AstronomyError) as error:
        q(h.connection()).small_body_history(
            h.NS, "2099 AB12", "2099-05-01", scopes=h.SCOPES
        )
    assert error.value.code == "not_ready"


def test_small_body_history_before_and_after_the_identification(world):
    before = q(world).small_body_history(
        h.NS, "2099 AB12", "2099-03-01", scopes=h.SCOPES
    )
    assert before["status"] == "answered" and before["identifications"] == []
    assert before["knowledge_cutoff"]["as_of"] == "2099-03-01" and isinstance(
        before["n"], int
    )
    after = q(world).small_body_history(h.NS, "K99A12B", "2099-04-30", scopes=h.SCOPES)
    [identification] = after["identifications"]
    assert (
        identification["identified_with"] == "2098 QX7"
        and identification["announced_in"] == "MPEC 2099-G42"
    )
    assert identification["citation"]["provider"] == "mpc-designations"
    assert "(999901)" in after["linked_designations"]
    early = q(world).small_body_history(
        h.NS, "2099 AB12", "2099-01-01", scopes=h.SCOPES
    )
    assert (
        early["status"] == "not_yet_published"
        and early["first_published_on"] == "2099-01-20"
    )
    assert (
        q(world).small_body_history(h.NS, "2099 ZZ9", "2099-05-01", scopes=h.SCOPES)[
            "status"
        ]
        == "unknown"
    )
    with pytest.raises(AstronomyError):
        q(world).small_body_history(
            h.NS, "2099 AB12", "2099-05-01", scopes={"knowledge:astronomy:read"}
        )


def test_orbit_solution_as_of_takes_the_latest_published_per_publisher(world):
    march = q(world).orbit_solution_as_of(
        h.NS, "2099 AB12", "2099-03-01", scopes=h.SCOPES, publisher="JPL"
    )
    current = march["solutions"]["JPL"]["current"]
    assert current["solution_id"] == "3" and current["epoch"]["jd"] == "2487737.5"
    assert current["arc"]["days"] == "14" and current["n_obs_used"] == 24
    assert march["other_publishers"]["MPC"]["current"]["solution_id"] == "E2099-B17"
    may = q(world).orbit_solution_as_of(
        h.NS, "2099 AB12", "2099-05-10", scopes=h.SCOPES, publisher="JPL"
    )
    assert may["solutions"]["JPL"]["current"]["solution_id"] == "12"
    assert [v["solution_id"] for v in may["solutions"]["JPL"]["earlier_vintages"]] == [
        "3"
    ]
    assert may["other_publishers"]["MPC"]["current"]["solution_id"] == "MPO999123"
    element = may["solutions"]["JPL"]["current"]["elements"]["a"]
    assert element["value"] == "2.19981" and element["unit"] == "au"
    # Only acquired data exists for a read: in May, with an acquisition cutoff before the JPL update.
    pinned = q(world).orbit_solution_as_of(
        h.NS,
        "2099 AB12",
        "2099-05-10",
        scopes=h.SCOPES,
        publisher="JPL",
        acquired_by_ms=h.ms("2099-05-01"),
    )
    assert pinned["solutions"]["JPL"]["current"]["solution_id"] == "3"


def test_exoplanet_status_across_a_false_positive_and_a_retraction(world):
    candidate = q(world).exoplanet_status_as_of(
        h.NS, "TOI-99902.01", "2099-03-01", scopes=h.SCOPES
    )
    [row] = candidate["dispositions"]
    assert (
        row["disposition"] == "candidate"
        and row["later_changes"][0]["disposition"] == "false_positive"
    )
    after = q(world).exoplanet_status_as_of(
        h.NS, "99902.01", "2099-05-15", scopes=h.SCOPES
    )
    assert after["dispositions"][0]["disposition"] == "false_positive"
    assert after["dispositions"][0]["later_changes"] == []
    confirmed = q(world).exoplanet_status_as_of(
        h.NS, "Fict-303 c", "2099-03-01", scopes=h.SCOPES
    )
    assert [
        (r["source_table"], r["disposition"]) for r in confirmed["dispositions"]
    ] == [("ps", "confirmed")]
    retracted = q(world).exoplanet_status_as_of(
        h.NS, "Fict-303 c", "2099-06-15", scopes=h.SCOPES
    )
    tables = {r["source_table"]: r for r in retracted["dispositions"]}
    assert (
        tables["removed"]["disposition"] == "retracted"
        and tables["removed"]["reference"]["doi"]
    )
    assert tables["ps"]["listing"] == {
        "state": "no_longer_listed",
        "observed_on": "2099-06-01",
    }
    named = q(world).exoplanet_status_as_of(
        h.NS, "Fict-101 b", "2099-04-01", scopes=h.SCOPES
    )
    assert {(r["source_table"], r["disposition"]) for r in named["dispositions"]} == {
        ("ps", "confirmed"),
        ("toi", "confirmed"),
    }


def test_launches_by_provider_site_and_window_with_outcomes_and_payloads(world):
    answer = q(world).launches(h.NS, scopes=h.SCOPES, provider="FICTSPACE")
    rows = {r["launch_tag"]: r for r in answer["launches"]}
    assert rows["2099-002"]["outcome"]["outcome"] == "failure"
    assert [p["name"] for p in rows["2099-001"]["payloads"]] == [
        "Fictsat 1",
        "Fictron 2 Stage 2",
    ]
    assert rows["2099-001"]["site"]["name"] == "Fictland Space Centre"
    window = q(world).launches(
        h.NS, scopes=h.SCOPES, date_from="2099-04-01", date_to="2099-04-30"
    )
    assert [r["launch_tag"] for r in window["launches"]] == ["2099-002", "2099-003"]
    assert q(world).launches(h.NS, scopes=h.SCOPES, provider="FICTSYS")["launches"][0][
        "outcome"
    ]["outcome"] == ("partial")
    early = q(world).launches(h.NS, scopes=h.SCOPES, site="FKSC", as_of="2099-01-01")
    assert early["status"] == "unknown" and early["launches"] == []


def test_orbital_object_history_keeps_catalogue_disagreements(world):
    answer = q(world).orbital_object_history(h.NS, "99901", scopes=h.SCOPES)
    decays = answer["disagreements"]["decay_date"]
    assert decays == {"celestrak-satcat": "2099-06-21", "gcat": "2099-06-20"}
    satcat = next(
        c for c in answer["catalogues"] if c["provider"] == "celestrak-satcat"
    )
    assert [r["status"] for r in satcat["revisions"]] == ["+", "D"]
    june = q(world).orbital_object_history(
        h.NS, "2099-001A", scopes=h.SCOPES, as_of="2099-06-10"
    )
    assert june["disagreements"] == {}
    assert q(world).orbital_object_history(h.NS, "99904", scopes=h.SCOPES)["conflicts"]


def test_space_weather_alerts_in_a_window_with_threads(world):
    answer = q(world).space_weather_alerts(
        h.NS, "2099-09-01", "2099-09-02", scopes=h.SCOPES
    )
    serials = {r["serial"]: r for r in answer["products"]}
    assert sorted(serials) == ["9001", "9002", "9003", "9004"]
    assert serials["9001"]["thread"] == {"cancelled_by": ["9004"]}
    assert serials["9002"]["thread"] == {"extended_by": ["9003"]}
    assert serials["9003"]["references"] == {
        "extends": {"serial": "9002", "in_record": True}
    }
    warnings = q(world).space_weather_alerts(
        h.NS, "2099-09-01", "2099-09-02", scopes=h.SCOPES, product_type="warning"
    )
    assert [r["serial"] for r in warnings["products"]] == ["9002", "9003"]
    strong = q(world).space_weather_alerts(
        h.NS, "2099-09-01", "2099-09-02", scopes=h.SCOPES, scale="G2"
    )
    assert [r["serial"] for r in strong["products"]] == ["9001"]
    # As of the first poll's cutoff the cancellation had not been issued.
    first = q(world).space_weather_alerts(
        h.NS, "2099-09-01", "2099-09-02", scopes=h.SCOPES, as_of="2099-09-01T13:30:00Z"
    )
    assert [r["serial"] for r in first["products"]] == ["9001", "9002"]
