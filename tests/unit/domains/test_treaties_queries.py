"""A treaty's status for a participant as of a date (#2620, TR08) and a participant's actions and a treaty's
reservations and objections (#2625, TR09)."""

from __future__ import annotations

import pytest

from src.kb.treaties_identity import TreatiesIdentity, place_key
from src.kb.treaties_queries import TreatiesQueries
from src.kb.treaties_records import TreatiesError, forbidden_keys
from tests.unit import treaties_harness as h


@pytest.fixture
def world():
    conn = h.connection()
    h.load_all(conn, observed_at_ms=1_000)
    yield conn, TreatiesQueries(conn)
    conn.close()


def test_status_as_of_uses_published_dates_and_cites_the_depositary_revision(world):
    _, ask = world
    before = ask.status_as_of(h.NS, "XXVII-99", "Exampland", "2091-01-01", scopes=h.READ_ONLY)
    (answer,) = before["answers"]
    assert [e["action_type"] for e in answer["chain"]] == ["signature"]
    assert answer["consent_to_be_bound_on_record"] is False and answer["later_actions"][0]["date"] == "2091-04-05"
    after = ask.status_as_of(h.NS, "treaties:untc:treaty:XXVII-99", "treaties:untc:participant:exampland",
                             "2095-01-01", scopes=h.READ_ONLY)
    (answer,) = after["answers"]
    assert [(e["action_type"], e["date_used"]) for e in answer["chain"]] == [("signature", "action_date"),
                                                                              ("ratification", "deposit_date")]
    assert answer["depositary_revision_used"] == "2099-01-15T09:15:00"
    assert answer["treaty"]["citation"]["revision_id"].startswith("treaty-rev:")
    (reservation,) = answer["statements"]
    assert reservation["statement_kind"] == "reservation" and reservation["text_verbatim"].startswith(
        "The Government of Exampland reserves")
    assert answer["notes_as_published"][0]["text_verbatim"].startswith("On 3 January 2099")
    assert answer["notes_as_published"][0]["after_as_of"] is True
    assert answer["effective_dates"][0]["note"].startswith("the source publishes no effective date")
    assert after["status"] == "actions_on_record" and not forbidden_keys(after)


def test_pending_and_unclear_are_returned_with_the_source_text(world):
    conn, ask = world
    h.apply(conn, "coe-treaty-office", v2=True, observed_at_ms=2_000)
    pending = ask.status_as_of(h.NS, "CETS 999", "Southland", "2099-06-01", scopes=h.READ_ONLY)
    assert pending["status"] == "pending"
    (answer,) = pending["answers"]
    assert [e["action_type"] for e in answer["chain"]] == ["signature", "ratification"]
    assert answer["later_actions"] == [{"record_key": "treaties:coe:action:999:southland:entry-into-force:1",
                                        "action_type": "entry-into-force", "date": "2099-09-01",
                                        "date_used": "effective_date"}]
    denounced = ask.status_as_of(h.NS, "999", "Northwind Republic", "2098-09-01", scopes=h.READ_ONLY)
    (answer,) = denounced["answers"]
    assert denounced["status"] == "pending" and answer["exit_on_record"] is True
    assert answer["pending"][0]["reason"].startswith("deposited on or before the as-of date")
    body = h.native_pages("untc-treaty-status")[0]["body"].replace("<td>3 Mar 2090</td>",
                                                                  "<td>date to be confirmed</td>")
    h.apply(conn, "untc-treaty-status", bodies={"status": body}, observed_at_ms=3_000)
    unclear = ask.status_as_of(h.NS, "XXVII-99", "Southland", "2095-01-01", scopes=h.READ_ONLY)
    assert unclear["status"] == "unclear"
    assert unclear["answers"][0]["unclear"][0]["source_text"] == "date to be confirmed"


def test_depositary_as_of_and_accepted_identity_reach_the_right_revision(world):
    conn, ask = world
    h.apply(conn, "untc-treaty-status", v2=True, observed_at_ms=2_000)
    now = ask.status_as_of(h.NS, "XXVII-99", "Northwind Republic", "2095-01-01", scopes=h.READ_ONLY)
    assert now["answers"][0]["chain"][0]["deposit_date"] == "2092-05-11"
    then = ask.status_as_of(h.NS, "XXVII-99", "Northwind Republic", "2095-01-01", scopes=h.READ_ONLY,
                            depositary_as_of="2099-03-01")
    assert then["answers"][0]["chain"][0]["deposit_date"] == "2092-05-10"
    assert then["answers"][0]["depositary_revision_used"] == "2099-01-15T09:15:00"
    oldland_then = ask.status_as_of(h.NS, "XXVII-99", "Oldland", "2095-01-01", scopes=h.READ_ONLY,
                                    depositary_as_of="2099-03-01")
    assert oldland_then["answers"][0]["chain"][0]["action_type"] == "succession"
    assert ask.status_as_of(h.NS, "XXVII-99", "Oldland", "2095-01-01", scopes=h.READ_ONLY)["status"] == \
        "no_action_on_record"
    places = h.seed_places(conn)
    identity = TreatiesIdentity(conn)
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace=h.NS)
    for candidate in result["candidates"]:
        if place_key(places["XNW"]) in candidate["records"]:
            identity.review(h.NS, candidate["candidate_id"], "accept", "reviewed", principal_id="reviewer",
                            scopes=h.REVIEW_SCOPES)
    via_place = ask.status_as_of(h.NS, "XXVII-99", place_key(places["XNW"]), "2095-01-01", scopes=h.SCOPES)
    (answer,) = via_place["answers"]
    assert answer["resolved_by"] == "accepted identity match" and answer["resolution_path"]
    with pytest.raises(TreatiesError):
        ask.status_as_of(h.NS, "XXVII-99", "Exampland", "someday", scopes=h.READ_ONLY)


def test_participant_actions_filter_by_type_period_and_source(world):
    _, ask = world
    everything = ask.participant_actions(h.NS, "Exampland", scopes=h.READ_ONLY)
    assert {a["provider"] for a in everything["actions"]} == {"untc", "coe-treaty-office"}
    assert all(a["citation"]["revision_id"] for a in everything["actions"])
    ratifications = ask.participant_actions(h.NS, "Exampland", scopes=h.READ_ONLY, action_types=["ratification"],
                                            providers=["coe-treaty-office"], include_statements=False)
    assert [(a["treaty_key"], a["date"]) for a in ratifications["actions"]] == [(h.COE, "2091-03-12")]
    period = ask.participant_actions(h.NS, "Exampland", scopes=h.READ_ONLY, period_start="2091-01-01",
                                     period_end="2091-12-31", include_statements=False)
    assert {a["date"] for a in period["actions"]} == {"2091-04-05", "2091-03-12", "2091-09-01"}
    nobody = ask.participant_actions(h.NS, "Nowhereland", scopes=h.READ_ONLY)
    assert nobody["status"] == "participant_not_on_record" and nobody["actions"] == []


def test_reservations_and_objections_are_verbatim_and_linked_where_the_source_links_them(world):
    _, ask = world
    answer = ask.treaty_statements(h.NS, "XXVII-99", scopes=h.READ_ONLY)
    by_kind = {s["statement_kind"]: s for s in answer["statements"]}
    assert set(by_kind) == {"reservation", "declaration", "objection"}
    objection = by_kind["objection"]
    assert objection["objects_to"]["link"] == "linked by the source"
    assert objection["objects_to"]["text_verbatim"] == by_kind["reservation"]["text_verbatim"]
    assert objection["objects_to"]["citation"]["revision_id"].startswith("treaty-rev:")
    coe = ask.treaty_statements(h.NS, "CETS 999", scopes=h.READ_ONLY, kinds=["objection"])
    (objection,) = coe["statements"]
    assert objection["objects_to"]["record_key"].endswith("coe-999-exampland-r1")
    assert objection["action_link_basis"] == "not stated by the source"
    only = ask.treaty_statements(h.NS, "CETS 999", scopes=h.READ_ONLY, participant="Exampland",
                                 kinds=["declaration"])
    assert only["statements"][0]["contact_details_withheld"] is True
    bundle = ask.evidence_bundle(answer)
    assert len(bundle["bibliography"]) == 3 and all("depositary revision" in b["text"] for b in bundle["bibliography"])
    assert ask.treaty_statements(h.NS, "XXVII-1", scopes=h.READ_ONLY)["status"] == "treaty_not_on_record"
