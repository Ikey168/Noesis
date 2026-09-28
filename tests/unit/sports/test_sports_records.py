"""Sports records and the revisioned store: round trip, revision append and order independence (#2137, SP02)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.ingestion.sports_sources import ACQUISITION_CONTRACT, to_ms
from src.kb.sports_records import (
    SportsError,
    forbidden_keys,
    register_schemas,
    validate_body,
)
from src.kb.sports_store import SportsStore
from tests.unit.sports import harness as h

FIXTURE = "sports:openfootball:fixture:exl-1:2099-00:matchday-1:a:b"


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def header(sha: str, published: str | None = None) -> dict:
    return {
        "contract": ACQUISITION_CONTRACT,
        "provider": "openfootball",
        "format": "openfootball-json",
        "file_sha256": sha * 64,
        "record_count": 1,
        "attribution": "openfootball / football.json (public domain)",
        "licence": {"id": "cc0-1.0"},
        "evidence_origin": "fixture",
        **({"published_at": published} if published else {}),
    }


def result(home: int, away: int, published: str) -> dict:
    return {
        "record_type": "match_result_revision",
        "record_key": FIXTURE,
        "source_record_id": "m1",
        "locator": "https://example.org/x.json#/matches/0",
        "published_at": published,
        "body": {"status": "official", "score": {"home": home, "away": away}},
    }


def apply(store, sha, observation, at="2099-09-10"):
    store.now = lambda: to_ms(at)
    return store.apply("global", header(sha), [observation], run_id="r", source_id="s")


def test_round_trip_carries_source_revision_published_and_retrieved_time(conn):
    h.load_league(conn)
    store = SportsStore(conn, initialize=False)
    (revision,) = store.history("global", "match_result_revision", h.fixture_key(101))
    assert revision["contract"] == "noesis-sports-record-v1"
    assert revision["body"] == {
        "periods": {"half_time": {"away": 0, "home": 1}},
        "duration": "REGULAR",
        "score": {"away": 1, "home": 2},
        "source_status": "FINISHED",
        "status": "official",
    }
    source = revision["source"]
    assert source["provider"] == "football-data" and source["source_record_id"] == "101"
    assert (
        source["locator"].endswith("#/matches/0")
        and source["evidence_origin"] == "fixture"
    )
    assert (
        source["attribution"] == "Football data provided by the Football-Data.org API"
    )
    assert revision["published_at"] == "2099-08-10T16:10:00Z"
    assert revision["retrieved_at_ms"] == to_ms("2099-08-18")
    assert store.revision("global", revision["revision_id"])["body"] == revision["body"]
    schema = json.loads(
        (
            h.ROOT / "contracts/schemas/jsonschema/noesis-sports-record-v1.json"
        ).read_text()
    )
    for kind in (
        "match_result_revision",
        "fixture_schedule_revision",
        "standing_snapshot",
        "team",
        "player",
    ):
        for key in store.records("global", kind):
            for item in store.labelled_history("global", kind, key):
                jsonschema.validate(item, schema)
                assert "None" not in json.dumps(item) and forbidden_keys(item) == []


def test_a_correction_appends_a_revision_and_never_overwrites(conn):
    store = SportsStore(conn)
    apply(store, "a", result(1, 1, "2099-08-17T16:00:00Z"))
    apply(store, "b", result(2, 1, "2099-08-24T11:00:00Z"))
    history = store.labelled_history("global", "match_result_revision", FIXTURE)
    assert [(r["status"], r["body"]["score"]["home"]) for r in history] == [
        ("official", 1),
        ("corrected", 2),
    ]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    assert (
        store.current("global", "match_result_revision", FIXTURE)["body"]["score"][
            "home"
        ]
        == 2
    )


def test_re_acquiring_unchanged_data_adds_nothing_and_a_reversion_is_a_new_correction(
    conn,
):
    store = SportsStore(conn)
    apply(store, "a", result(1, 1, "2099-08-17T16:00:00Z"))
    assert (
        apply(store, "a", result(1, 1, "2099-08-17T16:00:00Z"))["status"] == "unchanged"
    )
    # A new file with the same content and a later source date is the same revision.
    assert apply(store, "c", result(1, 1, "2099-08-18T09:00:00Z"))["revisions"] == 0
    apply(store, "b", result(2, 1, "2099-08-24T11:00:00Z"))
    reverted = apply(store, "d", result(1, 1, "2099-08-30T11:00:00Z"))
    assert reverted["revisions"] == 1
    labels = [
        r["status"]
        for r in store.labelled_history("global", "match_result_revision", FIXTURE)
    ]
    assert labels == ["official", "corrected", "corrected"]


def test_arrival_order_never_changes_keys_labels_or_current(conn):
    first, second = h.connection(), h.connection()
    a, b = SportsStore(first), SportsStore(second)
    apply(a, "a", result(1, 1, "2099-08-17T16:00:00Z"))
    apply(a, "b", result(2, 1, "2099-08-24T11:00:00Z"))
    # The later publication arrives first; the older one lands as history and is no false correction.
    apply(b, "b", result(2, 1, "2099-08-24T11:00:00Z"))
    apply(b, "a", result(1, 1, "2099-08-17T16:00:00Z"))

    def view(store):
        return [
            (r["revision_id"], r["status"])
            for r in store.labelled_history("global", "match_result_revision", FIXTURE)
        ]

    assert view(a) == view(b)
    assert (
        b.current("global", "match_result_revision", FIXTURE)["body"]["score"]["home"]
        == 2
    )
    # As of a date before the correction, only the original was published.
    assert b.current(
        "global", "match_result_revision", FIXTURE, cutoff_ms=to_ms("2099-08-20")
    )["body"]["score"] == {
        "home": 1,
        "away": 1,
    }
    # And only data acquired by a cutoff counts when one is given.
    b.now = lambda: to_ms("2099-09-10")
    assert (
        b.current(
            "global",
            "match_result_revision",
            FIXTURE,
            acquired_by_ms=to_ms("2099-09-01"),
        )
        is None
    )


def test_generation_changes_whenever_a_source_changes(conn):
    store = SportsStore(conn)
    empty = store.generation("global")
    apply(store, "a", result(1, 1, "2099-08-17T16:00:00Z"))
    one = store.generation("global")
    apply(
        store, "c", result(1, 1, "2099-08-18T09:00:00Z")
    )  # new file, unchanged record
    two = store.generation("global")
    assert len({empty, one, two}) == 3


def test_the_contract_refuses_odds_medical_data_extra_person_fields_and_none(conn):
    with pytest.raises(SportsError, match="odds"):
        validate_body(
            "match_result_revision",
            {"status": "official", "score": {"home": 1, "away": 0}, "odds": {}},
        )
    with pytest.raises(SportsError, match="published participant"):
        validate_body(
            "player",
            {"provider": "x", "name": "Jan Example", "nationality": "Exampleland"},
        )
    with pytest.raises(SportsError, match="odds, predictions or medical"):
        validate_body("player", {"provider": "x", "name": "Jan Example", "height": 188})
    with pytest.raises(SportsError, match="never None"):
        validate_body("team", {"provider": "x", "name": "None"})
    with pytest.raises(SportsError, match="deciding body"):
        validate_body("match_result_revision", {"status": "annulled"})
    with pytest.raises(SportsError, match="unknown schedule status"):
        validate_body("fixture_schedule_revision", {"status": "delayed"})
    store = SportsStore(conn)
    with pytest.raises(SportsError, match="partial page"):
        store.apply(
            "global",
            {**header("a"), "record_count": 2},
            [result(1, 1, "2099-08-17")],
            run_id="r",
            source_id="s",
        )


def test_schema_registers_in_the_schema_registry(conn):
    registered = register_schemas(
        conn,
        principal_id="operator",
        scopes={"operator", "knowledge:schema:register", "knowledge:schema:read"},
    )
    assert [r["name"] for r in registered] == ["noesis-sports-record"]
    again = register_schemas(
        conn, principal_id="operator", scopes={"operator", "knowledge:schema:register"}
    )
    assert again[0]["module_id"] == registered[0]["module_id"]


def test_readiness_is_not_ready_before_any_source_ran(conn):
    store = SportsStore(conn, initialize=False)
    assert store.ready() is False
    with pytest.raises(SportsError) as caught:
        SportsStore(conn).require_ready("global")
    assert caught.value.code == "not_ready"
