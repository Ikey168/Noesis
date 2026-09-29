"""Reviewable party, candidate and constituency identity and dated assertions across elections (#1957)."""

from __future__ import annotations

import pytest

from src.domains.political.model import record_alias, record_object
from src.kb.elections import ElectionError, constituency_record_key
from src.kb.elections_identity import ElectionIdentity
from src.kb.elections_polls import ElectionPolls
from src.kb.entity_history import EntityHistoryStore
from src.kb.ownership_store import OwnershipError
from tests.unit import elections_harness as h

BP_2099 = f"elections:{h.DE_ELECTION}:party:beispielpartei"
BP_2103 = f"elections:{h.DE_NEXT}:party:beispielpartei"
MU_2099 = f"elections:{h.DE_ELECTION}:party:musterunion"
NMU_2103 = f"elections:{h.DE_NEXT}:party:neue-musterunion"
SOURCE = {
    "url": "https://www.example.org/party-register/decision-1",
    "title": "Fictional party register notice",
}


@pytest.fixture()
def conn():
    connection = h.connection()
    h.apply(connection, "de-btw", h.DE_FINAL)
    h.apply_next_federal(connection)
    h.apply(connection, "gb", h.UK)
    yield connection
    connection.close()


def candidate(identity, left, right):
    return next(
        c
        for c in identity.candidates("global", scopes=h.REVIEW_SCOPES)
        if set(c["records"]) == {left, right}
    )


def test_labels_are_proposed_within_one_country_only(conn):
    identity = ElectionIdentity(conn, now=h.Clock())
    proposed = identity.propose("global", principal_id="alice", scopes=h.SCOPES)
    assert proposed["proposed"]
    pair = candidate(identity, BP_2099, BP_2103)
    assert pair["basis"] == "name-jurisdiction" and pair["state"] == "proposed"
    records = {r for c in proposed["candidates"] for r in c["records"]}
    assert not any(
        r.startswith(f"elections:{h.UK_ELECTION}") for r in records
    )  # never across countries
    assert not [
        c for c in proposed["candidates"] if set(c["records"]) == {MU_2099, NMU_2103}
    ]  # labels differ
    again = identity.propose("global", principal_id="alice", scopes=h.SCOPES)
    assert again["proposed"] == []  # idempotent
    # One Wahlkreis number in two boundary vintages is only an unqualified candidate.
    old = constituency_record_key("de-bt-wahlkreis", "001", "btw2025")
    new = constituency_record_key("de-bt-wahlkreis", "001", "btw2103")
    assert candidate(identity, old, new)["basis"] == "unqualified-identifier"


def test_poll_options_are_matched_to_result_lists_by_review(conn):
    ElectionPolls(conn).import_release(
        "global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url="https://www.beispiel-institut.example/sonntagsfrage.csv",
        csv_text=(h.FIXTURES / "poll_beispiel_institut_2099-02-21.csv").read_text(),
        redistribution="allowed",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    identity = ElectionIdentity(conn, now=h.Clock())
    identity.propose("global", principal_id="alice", scopes=h.SCOPES)
    poll = next(
        s
        for s in identity.subjects("global")
        if s["kind"] == "poll-option" and s["label"] == "Musterunion"
    )
    assert (
        candidate(identity, poll["record_key"], MU_2099)["basis"] == "name-jurisdiction"
    )


def test_accepting_and_reverting_a_decision_changes_and_restores_the_answer(conn):
    clock = h.Clock()
    identity = ElectionIdentity(conn, now=clock)
    before = identity.party_results("global", BP_2099, scopes=h.REVIEW_SCOPES)
    assert {r["election_id"] for r in before["results"]} == {h.DE_ELECTION}
    assert before["identity"]["state"] == "unmatched"
    identity.propose("global", principal_id="alice", scopes=h.SCOPES)
    pair = candidate(identity, BP_2099, BP_2103)
    accepted = identity.service.review(
        "global",
        pair["candidate_id"],
        "accept",
        "same registered party",
        principal_id="bob",
        scopes=h.REVIEW_SCOPES,
    )
    decision = (
        EntityHistoryStore(conn, initialize=False)
        .conn.execute(
            "SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
            [accepted["decision_id"]],
        )
        .fetchone()
    )
    assert decision == ("match",)
    joined = identity.party_results("global", BP_2099, scopes=h.REVIEW_SCOPES)
    assert {r["election_id"] for r in joined["results"]} == {h.DE_ELECTION, h.DE_NEXT}
    assert all(r["source_revision"]["file_sha256"] for r in joined["results"])
    identity.service.revert(
        "global",
        pair["candidate_id"],
        "wrong party",
        principal_id="bob",
        scopes=h.REVIEW_SCOPES,
    )
    after = identity.party_results("global", BP_2099, scopes=h.REVIEW_SCOPES)
    assert after["results"] == before["results"] and after["joined_records"] == [
        BP_2099
    ]
    assert (
        conn.execute(
            "SELECT count(*) FROM entity_identity_decisions WHERE decision_type='undo'"
        ).fetchone()[0]
        == 1
    )


def test_similar_names_to_canonical_entities_are_never_accepted(conn):
    from src.kb.entities import add_manual_alias

    canonical_id = add_manual_alias(conn, "Beispielpartei", "Beispielpartei", "ORG")
    identity = ElectionIdentity(conn, now=h.Clock())
    identity.propose("global", principal_id="alice", scopes=h.SCOPES)
    weak = candidate(identity, BP_2099, f"canonical:{canonical_id}")
    assert weak["basis"] == "similar-name"
    with pytest.raises(OwnershipError):
        identity.service.review(
            "global",
            weak["candidate_id"],
            "accept",
            "x",
            principal_id="b",
            scopes=h.REVIEW_SCOPES,
        )


def test_political_pack_aliases_resolve_only_within_their_jurisdiction(conn):
    record_object(
        conn,
        object_id="jurisdiction:de",
        object_type="jurisdiction",
        canonical_name="Germany",
    )
    record_object(
        conn,
        object_id="party:beispielpartei",
        object_type="party",
        canonical_name="Beispielpartei",
        jurisdiction_id="jurisdiction:de",
    )
    record_alias(conn, object_id="party:beispielpartei", alias="Beispielpartei")
    identity = ElectionIdentity(conn, now=h.Clock())
    identity.propose(
        "global",
        principal_id="alice",
        scopes=h.SCOPES,
        political_jurisdictions={"DE": "jurisdiction:de"},
    )
    assert (
        candidate(identity, BP_2099, "political:party:beispielpartei")["basis"]
        == "name-jurisdiction"
    )
    uk = f"elections:{h.UK_ELECTION}:party:example-party"
    found = identity.candidates("global", scopes=h.REVIEW_SCOPES, record_key=uk)
    assert all(not r.startswith("political:") for c in found for r in c["records"])


def test_successions_are_dated_cited_assertions_kept_side_by_side_and_retractable(conn):
    identity = ElectionIdentity(conn, now=h.Clock())
    renamed = identity.assert_relation(
        "global",
        kind="party_successor",
        subject_key=MU_2099,
        object_key=NMU_2103,
        valid_on="2101-06-01",
        source=SOURCE,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    other = identity.assert_relation(
        "global",
        kind="party_successor",
        subject_key=MU_2099,
        object_key=BP_2103,
        valid_on="2101-07-01",
        source={
            "url": "https://news.example.org/merger",
            "title": "Fictional merger report",
        },
        principal_id="alice",
        scopes=h.SCOPES,
    )
    lineage = identity.lineage("global", MU_2099, scopes=h.READ_ONLY)
    assert [a["assertion_id"] for a in lineage["assertions"]] == [
        renamed["assertion_id"],
        other["assertion_id"],
    ]
    (conflict,) = lineage["conflicts"]
    assert conflict["asserted_successors"] == sorted([NMU_2103, BP_2103])
    # Nothing merges: each record keeps its own results.
    assert {
        r["election_id"]
        for r in identity.party_results("global", MU_2099, scopes=h.READ_ONLY)[
            "results"
        ]
    } == {h.DE_ELECTION}
    identity.retract(
        "global",
        other["assertion_id"],
        "report withdrawn",
        principal_id="bob",
        scopes=h.SCOPES,
    )
    lineage = identity.lineage("global", MU_2099, scopes=h.READ_ONLY)
    assert [a["assertion_id"] for a in lineage["assertions"]] == [
        renamed["assertion_id"]
    ] and not lineage["conflicts"]
    assert identity.assert_relation(
        "global",
        kind="party_successor",
        subject_key=MU_2099,
        object_key=NMU_2103,
        valid_on="2101-06-01",
        source=SOURCE,
        principal_id="alice",
        scopes=h.SCOPES,
    )["idempotent"]


def test_boundary_changes_cite_the_official_redistricting_source_and_move_no_votes(
    conn,
):
    identity = ElectionIdentity(conn, now=h.Clock())
    old = constituency_record_key("de-bt-wahlkreis", "001", "btw2025")
    new = constituency_record_key("de-bt-wahlkreis", "001", "btw2103")
    with pytest.raises(ElectionError, match="official"):
        identity.assert_relation(
            "global",
            kind="constituency_successor",
            subject_key=old,
            object_key=new,
            valid_on="2101-01-01",
            source=SOURCE,
            principal_id="alice",
            scopes=h.SCOPES,
        )
    official = {
        **SOURCE,
        "official": True,
        "title": "Fictional Wahlkreiseinteilung 2103",
    }
    made = identity.assert_relation(
        "global",
        kind="constituency_successor",
        subject_key=old,
        object_key=new,
        valid_on="2101-01-01",
        source=official,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert made["state"] == "asserted" and "votes" not in str(made)
    with pytest.raises(ElectionError):
        identity.assert_relation(
            "global",
            kind="constituency_successor",
            subject_key=old,
            object_key=BP_2103,
            valid_on="2101-01-01",
            source=official,
            principal_id="alice",
            scopes=h.SCOPES,
        )
    with pytest.raises(ElectionError):
        identity.assert_relation(
            "global",
            kind="party_successor",
            subject_key=MU_2099,
            object_key=NMU_2103,
            valid_on="2101-01-01",
            source=SOURCE,
            principal_id="alice",
            scopes=h.READ_ONLY,
        )
