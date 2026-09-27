"""Answers: who declared an interest in a dossier, what a registrant declared, whom an official met (#1993)."""

from __future__ import annotations

import pytest

from src.kb.lobbying import LobbyingError, forbidden_keys
from src.kb.lobbying_identity import LobbyingIdentity
from src.kb.lobbying_links import LobbyingDossierLinks
from src.kb.lobbying_queries import LobbyingQueries
from tests.unit import lobbying_harness as h

REPORTS = {"knowledge:reports:write", "knowledge:reports:read"}


@pytest.fixture()
def world():
    conn = h.connection()
    h.load_all(conn)
    dossiers = h.dossiers(conn)
    scopes = h.REVIEW_SCOPES | dossiers["scopes"]
    for key in ("eu", "de"):
        LobbyingDossierLinks(conn).link_dossier(
            "global",
            h.DOSSIER_NS,
            dossiers[key]["dossier_id"],
            principal_id="alice",
            scopes=scopes,
        )
    yield conn, dossiers, scopes
    conn.close()


def test_dossier_interests_cite_register_revisions_and_mark_link_and_identity_state(
    world,
):
    conn, dossiers, scopes = world
    answer = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert forbidden_keys(answer) == []
    (row,) = answer["declared_interests"]
    assert (
        row["native_id"] == "000000000101-01"
        and row["identity"]["state"] == "unmatched"
    )
    assert [link["link_kind"] for link in row["links"]] == ["explicit-field"]
    assert (
        row["citation"]["register_revision_id"]
        and row["citation"]["source_revision"]["file_sha256"]
    )
    assert row["spend_ranges"] == [
        {
            "kind": "costs",
            "lower": "200000",
            "upper": "299999",
            "lower_open": False,
            "upper_open": False,
            "currency": "EUR",
            "period": {"start": "2098-01-01", "end": "2098-12-31"},
            "as_filed": "200 000 - 299 999",
            "party": None,
        }
    ]
    kinds = sorted(link["link_kind"] for m in answer["meetings"] for link in m["links"])
    assert kinds == ["explicit-field", "explicit-field", "unreviewed-candidate"]
    rapporteur = next(
        m for m in answer["meetings"] if m["official"]["role"] == "Rapporteur"
    )
    assert rapporteur["organisations"][0]["register_records"] == [
        "lobbying:eu-tr:000000000101-01"
    ]
    without = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
        include_candidates=False,
    )
    assert len(without["meetings"]) == 2


def test_as_of_returns_the_revision_in_force_then(world):
    conn, dossiers, scopes = world
    early = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
        as_of="2099-02-01",
    )
    (row,) = early["declared_interests"]
    assert (
        row["spend_ranges"][0]["lower"] == "100000"
        and row["effective_on"] == "2099-01-10"
    )
    assert early["meetings"] == []  # declared later
    before = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
        as_of="2098-01-01",
    )
    assert before["declared_interests"] == [] and before["not_in_force"]


def test_an_unmatched_registrant_and_reviewed_matches_side_by_side_are_never_reconciled(
    world,
):
    conn, dossiers, scopes = world
    identity = LobbyingIdentity(conn)
    proposed = identity.propose("global", principal_id="a", scopes=scopes)
    pair = next(
        c
        for c in proposed["candidates"]
        if c["records"]
        == ["lobbying:de-lobbyregister:R009901", "lobbying:eu-tr:000000000101-01"]
    )
    identity.service.review(
        "global",
        pair["candidate_id"],
        "accept",
        "DE entry states the TR number",
        principal_id="reviewer",
        scopes=scopes,
    )
    answer = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert answer["declared_interests"][0]["identity"]["state"] == "matched"
    (group,) = answer["side_by_side"]
    assert group["reconciled"] is False and {
        s["register"] for s in group["statements"]
    } == {"eu-tr", "de-lobbyregister"}
    ranges = {s["register"]: s["spend_ranges"][0] for s in group["statements"]}
    assert (
        ranges["eu-tr"]["kind"] == "costs"
        and ranges["de-lobbyregister"]["kind"] == "expenditure"
    )


def test_registrant_history_and_an_entity_gathering_two_registers(world):
    conn, dossiers, scopes = world
    queries = LobbyingQueries(conn)
    forum = queries.registrant_declarations(
        "global", scopes=scopes, register="eu-tr", native_id="000000000303-03"
    )
    (record,) = forum["registrants"]
    assert [r["change"] for r in record["history"]] == ["registered", "deregistered"]
    assert (
        record["history"][1]["ends_revision_id"]
        == record["history"][0]["citation"]["register_revision_id"]
    )
    assert record["identity"]["state"] == "unmatched"
    consultancy = queries.registrant_declarations(
        "global",
        scopes=scopes,
        register="eu-tr",
        native_id="000000000202-02",
        as_of="2099-02-01",
    )
    assert len(consultancy["registrants"][0]["in_force"]["clients"]) == 2
    identity = LobbyingIdentity(conn)
    proposed = identity.propose("global", principal_id="a", scopes=scopes)
    pair = next(
        c
        for c in proposed["candidates"]
        if c["records"]
        == ["lobbying:de-lobbyregister:R009902", "lobbying:eu-tr:000000000202-02"]
    )
    identity.service.review(
        "global",
        pair["candidate_id"],
        "accept",
        "stated TR number",
        principal_id="r",
        scopes=scopes,
    )
    gathered = queries.registrant_declarations(
        "global", scopes=scopes, entity_id=pair["entities"][1]
    )
    assert {r["entry"]["register"] for r in gathered["registrants"]} == {
        "eu-tr",
        "de-lobbyregister",
    }
    assert gathered["side_by_side"] and gathered["reconciled"] is False
    clients = {
        r["entry"]["register"]: {c["name"] for c in r["in_force"]["clients"]}
        for r in gathered["registrants"]
    }
    assert (
        clients["eu-tr"] != clients["de-lobbyregister"]
    )  # conflicting client lists kept as filed
    with pytest.raises(LobbyingError):
        queries.registrant_declarations("global", scopes=scopes)


def test_official_meetings_by_office_holder(world):
    conn, _dossiers, scopes = world
    queries = LobbyingQueries(conn)
    answer = queries.official_meetings(
        "global", scopes=scopes, official_id="ep-mep:990001"
    )
    assert [m["date"] for m in answer["meetings"]] == ["2099-02-03", "2099-02-05"]
    assert answer["meetings"][0]["dossier_links"][0]["link_kind"] == "explicit-field"
    by_name = queries.official_meetings(
        "global",
        scopes=scopes,
        official_name="robin example",
        date_from="2099-02-06",
        date_to="2099-02-06",
    )
    (meeting,) = by_name["meetings"]
    names = [o["name"] for o in meeting["organisations"]]
    assert names == ["Example Public Affairs SARL", "Sample Charging Operator SA"]
    assert meeting["organisations"][1]["identity"]["state"] == "unmatched"
    with pytest.raises(LobbyingError):
        queries.official_meetings(
            "global",
            scopes=h.READ_ONLY - {"knowledge:political:lobbying:read"},
            official_id="ep-mep:990001",
        )


def test_the_answer_exports_as_a_cited_authored_report(world):
    conn, dossiers, scopes = world
    scopes = scopes | REPORTS | {f"namespace:{h.NS}:read"}
    out = LobbyingQueries(conn).export_report(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        "grid-bundle",
        principal_id="alice",
        scopes=scopes,
    )
    report = out["report"]
    sections = {s["id"]: s for s in report["content"]["sections"]}
    assert (
        len(sections["declared-interests"]["assertions"]) == 1
        and len(sections["meetings"]["assertions"]) == 2
    )
    assertion = sections["declared-interests"]["assertions"][0]
    assert (
        "EUR 200000-299999" in assertion["text"]
        and assertion["dependencies"][0]["namespace"] == "global"
    )
    assert {b["id"] for b in report["content"]["bibliography"]} >= set(
        assertion["citations"]
    )
    assert forbidden_keys(report) == []


def test_the_report_generation_moves_when_any_register_gains_an_export(world):
    conn, dossiers, scopes = world
    scopes = scopes | REPORTS | {f"namespace:{h.NS}:read"}
    queries = LobbyingQueries(conn)

    def snapshot(key):
        return queries.export_report(
            "global",
            h.DOSSIER_NS,
            dossiers["eu"]["dossier_id"],
            key,
            principal_id="alice",
            scopes=scopes,
        )["report"]["content"]["snapshot"]

    before = snapshot("before")
    # The EU register has two exports; the UK register only one. A second UK export must still be visible.
    body = (h.FIXTURES / "uk_orcl_2099-04-30.csv").read_text() + (
        "ORCL0099,Example Public Affairs Ltd,09990099,1 Example Street London,2099 Q2,Other Example GmbH,2099-07-20\n"
    )
    assert h.apply(conn, "uk-orcl", "uk2.csv", body=body)["status"] == "applied"
    after = snapshot("after")
    assert after["generations"]["global"] == before["generations"]["global"] + 1
    assert after["id"] != before["id"]
