"""Election record owner: idempotency, separate vintages, as-of by source date, corrections and schemas (#1923)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.elections import (
    CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    ElectionError,
    ElectionStore,
    authorize,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from tests.unit import elections_harness as h

SCHEMA = json.loads(
    (h.ROOT / "contracts/schemas/jsonschema/noesis-election-record-v1.json").read_text()
)
TABLES = (
    "election_releases",
    "election_elections",
    "election_constituencies",
    "election_contests",
    "election_candidates",
    "election_result_vintages",
    "election_release_members",
)


def state(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in TABLES
    }


def valid(view):
    errors = list(Draft7Validator(SCHEMA).iter_errors(view))
    assert not errors, errors
    return True


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def test_scopes_contract_and_namespace_access():
    assert (READ_SCOPE, WRITE_SCOPE) == (
        "knowledge:political:elections:read",
        "knowledge:political:elections:write",
    )
    assert CONTRACT == "noesis-election-record-v1"
    with pytest.raises(ElectionError):
        authorize("global", {READ_SCOPE}, READ_SCOPE)
    authorize("global", h.READ_ONLY, READ_SCOPE)
    with pytest.raises(ElectionError):
        authorize("global", h.READ_ONLY, WRITE_SCOPE, write=True)


def test_re_ingesting_an_unchanged_file_adds_nothing_whatever_the_fetch_time(conn):
    first = h.apply(conn, "de-btw", h.DE_PRELIMINARY, run_id="a")
    assert (
        first["status"] == "applied"
        and first["vintages"] == 8
        and first["preliminary"] == 8
    )
    before = state(conn)
    again = h.apply(conn, "de-btw", h.DE_PRELIMINARY, run_id="b")
    assert again["status"] == "unchanged" and state(conn) == before
    # The same figures in a re-serialised file record the release but add no vintage.
    body = (
        (h.FIXTURES / h.DE_PRELIMINARY)
        .read_text()
        .replace("# Authored fixture", "#  Authored fixture")
    )
    reserialized = h.apply(conn, "de-btw", "x", body=body)
    assert reserialized["status"] == "applied" and reserialized["vintages"] == 0
    assert state(conn)["election_result_vintages"] == before["election_result_vintages"]


def test_preliminary_and_certified_are_separate_vintages_and_as_of_follows_source_dates(
    conn,
):
    store = ElectionStore(conn)
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    final = h.apply(conn, "de-btw", h.DE_FINAL)
    assert final["certified"] == 8
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    history = store.history("global", contest)
    assert [v["kind"] for v in history] == ["preliminary", "certified"]
    assert history[1]["previous_vintage_id"] == history[0]["vintage_id"]
    assert store.in_force("global", contest, "2099-03-10")["kind"] == "preliminary"
    assert store.in_force("global", contest, "2099-03-20")["kind"] == "certified"
    assert store.in_force("global", contest, "2099-03-01") is None
    answer = store.results("global", contest)
    assert answer["status"] == "certified" and answer["current"]["figures"][
        "remarks"
    ] == ["Nachzählung"]
    (change,) = answer["changes"]
    assert change["transition"] == "preliminary->certified"
    changed = {
        (c["entry"], c["field"]): (c["before"], c["after"])
        for c in change["changed_figures"]
    }
    assert changed[("party:beispielpartei", "votes")] == (41000, 40990)
    assert all(
        "difference" not in c for c in change["changed_figures"]
    )  # values as published, nothing computed
    assert change["from"]["source_revision"]["published_on"] == "2099-03-02"
    assert change["to"]["source_revision"]["published_on"] == "2099-03-20"
    for view in history:
        assert valid(view)
    assert (
        valid(answer["contest"])
        and valid(answer["constituency"])
        and valid(answer["election"])
    )
    assert forbidden_keys(answer) == []


def test_a_late_older_preliminary_file_lands_as_history_not_as_the_current_result(conn):
    store = ElectionStore(conn)
    h.apply(conn, "de-btw", h.DE_FINAL)
    late = h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    assert late["preliminary"] == 8
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    assert store.in_force("global", contest)["kind"] == "certified"
    assert [v["kind"] for v in store.history("global", contest)] == [
        "preliminary",
        "certified",
    ]
    assert store.in_force("global", contest, "2099-03-05")["kind"] == "preliminary"


def test_a_changed_certified_figure_is_a_correction_and_never_overwrites(conn):
    store = ElectionStore(conn)
    h.apply(conn, "de-btw", h.DE_FINAL)
    body = (
        (h.FIXTURES / h.DE_FINAL)
        .read_text()
        .replace("Stand: 20.03.2099 10:00", "Stand: 02.05.2099 09:00")
        .replace(";Musterunion;2;1;39020;", ";Musterunion;2;1;39021;")
    )
    corrected = h.apply(conn, "de-btw", "corrected", body=body)
    assert corrected["corrected"] == 1 and corrected["vintages"] == 1
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    kinds = [(v["kind"], v["declared_kind"]) for v in store.history("global", contest)]
    assert kinds == [("certified", "certified"), ("corrected", "certified")]
    assert store.in_force("global", contest, "2099-04-01")["kind"] == "certified"


def test_constituencies_carry_official_numbers_and_geometry_references(conn):
    store = ElectionStore(conn)
    h.load_results(conn)
    (wk,) = store.find_constituency("global", "de-bt-wahlkreis", "1")
    assert wk["native_id"] == "001" and wk["boundary_vintage"] == "btw2025"
    assert (
        wk["geometry_ref"]["layer"] == "btw25_geometrie_wahlkreise_shp"
        and wk["geometry_ref"]["native_id"] == "001"
    )
    (county,) = store.find_constituency("global", "us-fips-county", "99001.0")
    assert county["geometry_ref"]["vintage"] == "tiger2020"
    (ons,) = store.find_constituency("global", "gb-ons-pcon", "e14099901")
    assert ons["geometry_ref"]["crs"] == "EPSG:27700" and valid(ons)
    assert not store.find_constituency(
        "global", "de-land", "1"
    )  # a Land is an area, not a constituency
    uk = store.contests("global", election_id=h.UK_ELECTION)
    assert {c["rules"]["system"] for c in uk} == {"first-past-the-post"}
    assert {
        c["rules"]["system"]
        for c in store.contests("global", election_id=h.DE_ELECTION)
    } == {"mixed-member proportional"}
    lists = store.candidates("global", election_id=h.UK_ELECTION)
    assert {c["kind"] for c in lists} == {"candidate", "list"}
    assert all(c["record_key"].startswith(f"elections:{h.UK_ELECTION}:") for c in lists)


def test_release_is_all_or_nothing_and_rejects_derived_fields(conn):
    fetched, item = h.page("de-btw", h.DE_PRELIMINARY)
    store = ElectionStore(conn)
    header = fetched.records[0]["election_release"]
    contests = [r["election_contest"] for r in fetched.records]
    with pytest.raises(ElectionError) as exc:
        store.apply_release(
            "global", header, contests[:-1], run_id="r", source_id=item["source_id"]
        )
    assert exc.value.code == "incomplete_release"
    tampered = [dict(c, figures={**c["figures"], "prediction": 1}) for c in contests]
    with pytest.raises(ElectionError):
        store.apply_release(
            "global", header, tampered, run_id="r", source_id=item["source_id"]
        )
    assert not conn.execute("SELECT count(*) FROM election_releases").fetchone()[0]


def test_readiness_and_feature_flag_default_off(conn):
    report = readiness(conn)
    assert report["enabled"] is False and report["store_ready"] is False
    h.apply(conn, "gb", h.UK)
    report = readiness(conn)
    assert (
        report["store_ready"]
        and report["providers"]["uk-electoral-commission"]["releases"] == 1
    )
    assert report["providers"]["wahlrecht-de"]["live"] == "not-implemented"
    assert feature_enabled(conn) is False
    release = ElectionStore(conn).release(
        "global", conn.execute("SELECT release_id FROM election_releases").fetchone()[0]
    )
    assert valid(release) and release["evidence_origin"] == "fixture"
