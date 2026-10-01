"""A treaty's status for a participant as of a date (#2620) and a participant's actions and a treaty's statements
(#2625)."""

from __future__ import annotations

import json

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.treaties_identity import TreatiesIdentity
from src.kb.treaties_queries import TreatyQueries
from src.kb.treaties_records import (
    TreatiesError,
    forbidden_keys,
    minimisation_violations,
)
from tests.unit import treaties_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    h.load_all(connection, v2=True)
    yield connection
    connection.close()


def source_for(answer, key):
    return next(s for s in answer["sources"] if s["treaty"]["treaty_key"] == key)


def test_status_uses_published_dates_and_returns_pending_and_unclear_items(conn):
    queries = TreatyQueries(conn)
    answer = queries.status_as_of(h.NS, "cets:990", "Germany", "2100-03-01", scopes=h.READ_ONLY)
    coe = source_for(answer, h.COE_TREATY)
    assert coe["record_state"] == "denunciation-or-withdrawal-deposited-effective-later"
    assert [p["effective_date"] for p in coe["pending"]] == ["2100-05-01"]
    assert "not a statement of legal status" in coe["record_state_basis"]
    assert all(item["citation"]["depositary_revision"].startswith("Status as of") for item in coe["chain"])
    assert coe["pending"][0]["citation"]["depositary_revision"] == "Status as of 15/04/2100"
    ratification = next(c for c in coe["chain"] if c["action_type"] == "ratification")
    assert ratification["citation"]["depositary_revision"] == "Status as of 30/09/2099"  # unchanged since then
    earlier = queries.status_as_of(h.NS, "cets:990", "Germany", "2099-06-01", scopes=h.READ_ONLY)
    assert source_for(earlier, h.COE_TREATY)["record_state"] == "consent-and-entry-into-force-on-record"
    before = queries.status_as_of(h.NS, "cets:990", "Germany", "2098-10-01", scopes=h.READ_ONLY)
    assert source_for(before, h.COE_TREATY)["record_state"] == "signature-only-on-record"
    examplestan = queries.status_as_of(h.NS, "untc:XXIX-99", "Examplestan", "2100-03-01", scopes=h.READ_ONLY)
    untc = source_for(examplestan, h.UNTC_TREATY)
    assert untc["record_state"] == "consent-to-be-bound-on-record-effective-date-not-published"
    (unclear,) = untc["unclear"]
    assert unclear["action_type"] == "reservation" and unclear["section_note"].startswith("(Unless otherwise")
    assert "withdrawal" in [c["action_type"] for c in untc["chain"]]  # a statement withdrawal is not an exit


def test_answers_cite_the_depositary_revision_known_at_a_date(conn):
    queries = TreatyQueries(conn)
    latest = queries.status_as_of(h.NS, "untc:XXIX-99", "Germany", "2099-01-01", scopes=h.READ_ONLY)
    ratification = next(c for c in source_for(latest, h.UNTC_TREATY)["chain"] if c["action_type"] == "ratification")
    assert ratification["action_date"] == "2098-09-04" and ratification["citation"]["revision_no"] == 2
    known = queries.status_as_of(h.NS, "untc:XXIX-99", "Germany", "2099-01-01", scopes=h.READ_ONLY,
                                 known_as_of="2100-01-01")
    ratification = next(c for c in source_for(known, h.UNTC_TREATY)["chain"] if c["action_type"] == "ratification")
    assert ratification["action_date"] == "2098-09-03" and ratification["later_revisions"] == 1
    removed = queries.status_as_of(h.NS, "untc:XXIX-99", "Examplonia", "2099-01-01", scopes=h.READ_ONLY)
    assert source_for(removed, h.UNTC_TREATY)["removed_by_source"][0]["citation"]["change"] == "removed-by-source"
    assert removed["status"] == "no_action_on_record"


def test_participant_actions_filter_by_period_type_and_source(conn):
    queries = TreatyQueries(conn)
    answer = queries.participant_actions(h.NS, "Germany", scopes=h.READ_ONLY, date_from="2098-01-01",
                                         date_to="2099-12-31", source="coe-treaty-office")
    assert {a["provider"] for a in answer["actions"]} == {"coe-treaty-office"}
    assert [a["action_type"] for a in answer["actions"]] == ["signature", "declaration", "ratification",
                                                             "entry-into-force"]
    only = queries.participant_actions(h.NS, "Germany", scopes=h.READ_ONLY, action_types=["denunciation"])
    assert {a["action_type"] for a in only["actions"]} == {"denunciation"}
    with pytest.raises(TreatiesError):
        queries.participant_actions(h.NS, "Germany", scopes=h.READ_ONLY, action_types=["obligation"])
    nobody = queries.participant_actions(h.NS, "Atlantis", scopes=h.READ_ONLY)
    assert nobody["status"] == "no_action_on_record" and nobody["actions"] == []


def test_statements_are_verbatim_and_objections_link_as_published(conn):
    queries = TreatyQueries(conn)
    answer = queries.treaty_statements(h.NS, "untc:XXIX-99", scopes=h.READ_ONLY)
    by_type = {s["action_type"]: s for s in answer["statements"]}
    reservation, objection = by_type["reservation"], by_type["objection"]
    assert reservation["text_verbatim"].startswith("Reservation: Examplestan does not consider itself bound")
    assert objection["objected"]["objected_statement"]["action_key"] == reservation["action_key"]
    assert reservation["objections_linked"][0]["action_key"] == objection["action_key"]
    coe = queries.treaty_statements(h.NS, "cets:990", scopes=h.READ_ONLY, kinds=["reservation"],
                                    participant="France")
    (statement,) = coe["statements"]
    assert statement["text_verbatim"].startswith("In accordance with article 25")
    assert statement["objections_linked"] == [] and "objected" not in statement


def test_accepted_matches_widen_treaty_and_participant_resolution(conn):
    places = h.seed_places(conn)
    identity = TreatiesIdentity(conn, now=h.Clock())
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    for candidate in proposed["candidates"]:
        if candidate["subject"].endswith(":germany") or candidate["kind"] == "treaty":
            identity.review(h.NS, candidate["candidate_id"], "accept", "reviewed", principal_id="bob",
                            scopes=h.REVIEW_SCOPES)
    queries = TreatyQueries(conn)
    answer = queries.status_as_of(h.NS, "celex:22099A0101(01)", "iso3166:DE", "2100-03-01", scopes=h.READ_ONLY)
    assert {s["treaty"]["treaty_key"] for s in answer["sources"]} == {h.CELLAR_TREATY, h.COE_TREATY}
    assert answer["connected_by"]["treaty"]["accepted_treaty_matches"][0]["method"] == "published-cross-reference"
    assert set(answer["participant_keys"]) == {"treaties:participant:coe:germany", "treaties:participant:untc:germany"}
    assert places["DE"] in json.dumps(answer["connected_by"])


def test_answers_carry_exclusions_and_export_a_cited_bundle(conn):
    queries = TreatyQueries(conn)
    answer = queries.status_as_of(h.NS, "untc:XXIX-99", "Examplestan", "2100-03-01", scopes=h.READ_ONLY)
    assert forbidden_keys(answer) == [] and minimisation_violations(answer) == []
    assert "no legal advice" in answer["exclusions"] and "not legal advice" in answer["notice"]
    bundle = queries.export_bundle(answer, created_at_ms=1)
    verified = verify_bundle(bundle)
    assert verified.errors == [] and bundle["completeness"]["status"] == "partial"
    evidence = [o["payload"] for o in bundle["objects"] if o["type"] == "evidence"]
    assert evidence and all({"source", "record_revision", "as_of"} <= set(e) for e in evidence)
    assert all(e["record_revision"]["revision_id"] and e["as_of"]["retrieved_at_ms"] for e in evidence)
    missing = queries.status_as_of(h.NS, "cets:1", "Germany", "2100-01-01", scopes=h.READ_ONLY)
    assert missing["status"] == "no_treaty_on_record"
    assert verify_bundle(queries.export_bundle(missing, created_at_ms=1)).errors == []
    with pytest.raises(TreatiesError):
        queries.status_as_of(h.NS, "cets:990", "Germany", "2100-01-01", scopes={"knowledge:legal:read"})
