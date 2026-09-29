"""Lobbying record owner: validation, idempotency, revision chaining and lifecycle revisions (#1934)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.lobbying import (
    CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    LobbyingError,
    LobbyingStore,
    authorize,
    feature_enabled,
)
from tests.unit import lobbying_harness as h

SCHEMA = json.loads(
    (h.ROOT / "contracts/schemas/jsonschema/noesis-lobbying-record-v1.json").read_text()
)
TABLES = (
    "lobbying_exports",
    "lobbying_entries",
    "lobbying_revisions",
    "lobbying_export_members",
)


def state(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in TABLES
    }


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def test_scopes_and_contract_names():
    assert (READ_SCOPE, WRITE_SCOPE) == (
        "knowledge:political:lobbying:read",
        "knowledge:political:lobbying:write",
    )
    assert CONTRACT == "noesis-lobbying-record-v1"
    with pytest.raises(LobbyingError):
        authorize(
            "global", {READ_SCOPE}, READ_SCOPE
        )  # namespace access is required too
    authorize("global", h.READ_ONLY, READ_SCOPE)
    with pytest.raises(LobbyingError):
        authorize("global", h.READ_ONLY, WRITE_SCOPE, write=True)


def test_re_ingesting_an_unchanged_export_adds_nothing_whatever_the_fetch_time(conn):
    first = h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml", run_id="a")
    assert first["registered"] == 3
    before = state(conn)
    again = h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml", run_id="b")
    assert again["status"] == "unchanged" and state(conn) == before
    # The same statements in a re-serialized file (new digest) add an export but no revision.
    body = (
        (h.FIXTURES / "eu_tr_2099-01-15.xml")
        .read_text()
        .replace("\n  <interest", "\n    <interest")
    )
    reserialized = h.apply(conn, "eu-tr", "x.xml", body=body)
    assert reserialized["status"] == "applied" and not any(
        reserialized[c] for c in ("registered", "amended")
    )
    assert state(conn)["lobbying_revisions"] == before["lobbying_revisions"]


def test_changed_entries_chain_revisions_and_deregistration_is_a_lifecycle_revision(
    conn,
):
    store = LobbyingStore(conn)
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    result = h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    assert (result["amended"], result["deregistered"]) == (2, 1)
    first, second = store.history("global", h.entry_id(conn, h.EU_ASSOC))
    assert (
        second["previous_revision_id"] == first["revision_id"]
        and second["change"] == "amended"
    )
    assert [s["lower"] for s in store.spend(first)] == ["100000"] and [
        s["lower"] for s in store.spend(second)
    ] == ["200000"]
    registered, deregistered = store.history("global", h.entry_id(conn, h.EU_FORUM))
    assert (
        deregistered["lifecycle"] == "deregistered"
        and deregistered["statement"] is None
    )
    assert deregistered["previous_revision_id"] == registered["revision_id"]
    assert deregistered["change_basis"] == "absent from a newer full register export"
    assert (
        store.entry("global", h.entry_id(conn, h.EU_FORUM))["native_id"]
        == "000000000303-03"
    )  # never deleted
    for view in (first, second, deregistered):
        assert not list(Draft7Validator(SCHEMA).iter_errors(view))
    for view in store.spend(second) + store.clients(second) + store.interests(second):
        assert not list(Draft7Validator(SCHEMA).iter_errors(view))


def test_an_entry_that_reappears_is_reregistered(conn):
    store = LobbyingStore(conn)
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    body = (
        (h.FIXTURES / "eu_tr_2099-01-15.xml")
        .read_text()
        .replace('generationDate="2099-01-15"', 'generationDate="2099-05-01"')
    )
    back = h.apply(conn, "eu-tr", "y.xml", body=body)
    assert back["reregistered"] == 1
    history = store.history("global", h.entry_id(conn, h.EU_FORUM))
    assert [r["change"] for r in history] == [
        "registered",
        "deregistered",
        "reregistered",
    ]


def test_an_older_export_arriving_later_is_a_dated_observation_that_deregisters_nothing(
    conn,
):
    store = LobbyingStore(conn)
    h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    late = h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    assert (
        late["newest"] is False
        and late["deregistered"] == 0
        and late["registered"] == 1
    )
    history = store.history("global", h.entry_id(conn, h.EU_ASSOC))
    assert [r["effective_on"] for r in history] == [
        "2099-01-10",
        "2099-02-20",
    ]  # the register's own date order
    assert (
        store.in_force("global", h.entry_id(conn, h.EU_ASSOC), "2099-02-01")[
            "effective_on"
        ]
        == "2099-01-10"
    )
    assert (
        store.in_force("global", h.entry_id(conn, h.EU_ASSOC))["effective_on"]
        == "2099-02-20"
    )
    assert store.in_force("global", h.entry_id(conn, h.EU_ASSOC), "2098-01-01") is None
    assert "older" in history[0]["change_basis"]


def test_conflicting_declarations_from_two_registers_are_stored_side_by_side(conn):
    store = LobbyingStore(conn)
    h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    h.apply(conn, "de-lobbyregister", "de_lobbyregister_2099-02-01.json")
    eu = store.in_force("global", h.entry_id(conn, h.EU_CONSULTANCY))
    de = store.in_force("global", h.entry_id(conn, h.DE_CONSULTANCY))
    assert eu["entry_id"] != de["entry_id"] and eu["source_id"] != de["source_id"]
    assert {c["name"] for c in store.clients(eu)} != {
        c["name"] for c in store.clients(de)
    }


def test_uk_quarterly_returns_are_revisions_and_meetings_are_entries(conn):
    store = LobbyingStore(conn)
    h.apply(conn, "uk-orcl", "uk_orcl_2099-04-30.csv")
    q4, q1 = store.history("global", h.entry_id(conn, h.UK_CONSULTANCY))
    assert (q4["native_version"], q1["native_version"]) == ("2098-Q4", "2099-Q1")
    assert q1["previous_revision_id"] == q4["revision_id"]
    h.apply(conn, "ep-meetings", "ep_meetings_2099-02-10.csv")
    meetings = store.entries("global", kind="meeting")
    assert len(meetings) == 3
    meeting = store.meeting(store.in_force("global", meetings[0]["entry_id"]))
    assert (
        meeting["record_type"] == "meeting"
        and meeting["citation"]["source_revision"]["evidence_origin"] == "fixture"
    )


def test_partial_exports_point_values_and_unknown_registers_are_refused(conn):
    store = LobbyingStore(conn)
    page, _ = h.page("eu-tr", "eu_tr_2099-01-15.xml")
    header = dict(page.records[0]["lobbying_export"])
    entries = [dict(r["lobbying_entry"]) for r in page.records]
    with pytest.raises(LobbyingError) as partial:
        store.apply_export("global", header, entries[:2], run_id="r", source_id="s")
    assert partial.value.code == "incomplete_export"
    pointy = json.loads(json.dumps(entries))
    pointy[0]["spend"][0]["midpoint"] = "150000"
    with pytest.raises(LobbyingError):
        store.apply_export("global", header, pointy, run_id="r", source_id="s")
    with pytest.raises(LobbyingError):
        store.apply_export(
            "global",
            {**header, "register": "nowhere"},
            entries,
            run_id="r",
            source_id="s",
        )
    assert not conn.execute("SELECT count(*) FROM lobbying_revisions").fetchone()[0]


def test_projection_is_namespaced_and_the_feature_defaults_off(conn):
    item = h.source("eu-transparency-register")
    item["lobbying"]["namespace"] = "team"
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml", item=item)
    assert conn.execute(
        "SELECT DISTINCT namespace FROM lobbying_revisions"
    ).fetchall() == [("team",)]
    assert feature_enabled(conn) is False


def test_absence_deregisters_only_entries_within_a_bounded_selection(conn):
    store = LobbyingStore(conn)
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    item = h.source("eu-transparency-register")
    item["lobbying"]["selection"] = {
        "native_ids": ["000000000101-01", "000000000202-02"]
    }
    result = h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml", item=item)
    assert (
        result["deregistered"] == 0
    )  # the forum is outside the selection, so its absence says nothing
    assert (
        store.in_force("global", h.entry_id(conn, h.EU_FORUM))["lifecycle"] == "active"
    )


def test_declared_grants_are_only_candidates_for_funding_programme_records(conn):
    from src.kb.funding_opportunities import FundingOpportunityStore
    from src.kb.funding_records import record
    from tests.unit.funding.harness import NS as FUNDING_NS
    from tests.unit.funding.harness import SCOPES as FUNDING_SCOPES

    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    programme = record(
        "exist",
        "programme",
        "frp",
        "Fictional Research Programme",
        source_url="https://www.exist.de/fictional",
        authority={"kind": "funder", "name": "Fixture"},
        status={"asserted": "rolling"},
    )
    FundingOpportunityStore(conn).ingest(
        FUNDING_NS,
        "exist",
        [programme],
        observation_id="o",
        observed_at_ms=1,
        scopes=FUNDING_SCOPES,
    )
    store = LobbyingStore(conn)
    revision = store.in_force("global", h.entry_id(conn, h.EU_ASSOC))
    (candidate,) = store.grant_candidates(
        "global",
        revision["revision_id"],
        funding_namespace=FUNDING_NS,
        scopes=FUNDING_SCOPES,
    )
    assert candidate["state"] == "candidate" and candidate["confirmed"] is False
    assert candidate["funding_record"]["title"] == "Fictional Research Programme"
    assert (
        candidate["declared_grant"]["citation"]["revision_id"]
        == revision["revision_id"]
    )


def test_a_late_older_export_lands_as_history_without_changing_the_current_state(conn):
    store = LobbyingStore(conn)
    january = (h.FIXTURES / "eu_tr_2099-01-15.xml").read_text()
    march = (h.FIXTURES / "eu_tr_2099-03-01.xml").read_text()
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    h.apply(
        conn, "eu-tr", "eu_tr_2099-03-01.xml"
    )  # the forum is deregistered by absence
    forum = h.entry_id(conn, h.EU_FORUM)
    deregistered = store.in_force("global", forum)
    assert deregistered["lifecycle"] == "deregistered"
    # A February export (older than March) arrives late with a distinct forum statement.
    february = (
        january.replace('generationDate="2099-01-15"', 'generationDate="2099-02-01"')
        .replace(
            "<lastUpdateDate>2098-12-01</lastUpdateDate>",
            "<lastUpdateDate>2099-01-20</lastUpdateDate>",
        )
        .replace("electric vehicle charging networks", "charging networks")
    )
    late = h.apply(conn, "eu-tr", "feb.xml", body=february)
    assert late["newest"] is False and late["amended"] >= 1
    assert store.in_force("global", forum)["revision_id"] == deregistered["revision_id"]
    history = store.history("global", forum)
    assert [r["change"] for r in history] == ["registered", "amended", "deregistered"]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    # A later full export that still omits the forum adds no second deregistration.
    may = march.replace('generationDate="2099-03-01"', 'generationDate="2099-05-01"')
    assert h.apply(conn, "eu-tr", "may.xml", body=may)["deregistered"] == 0
    assert len(store.history("global", forum)) == 3
    # Re-listed in a newer export: reregistered, not amended.
    june = january.replace('generationDate="2099-01-15"', 'generationDate="2099-06-01"')
    assert h.apply(conn, "eu-tr", "june.xml", body=june)["reregistered"] == 1
    current = store.in_force("global", forum)
    assert (
        current["change"] == "reregistered"
        and current["previous_revision_id"] == deregistered["revision_id"]
    )
