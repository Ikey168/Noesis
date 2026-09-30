"""Actions for an entity and its group as of a date; actions by authority and legal basis (#2651, EN09, EN10)."""

from __future__ import annotations

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.enforcement import EnforcementError, forbidden_keys
from src.kb.enforcement_queries import (
    action_history,
    actions_by_authority,
    actions_for_entity,
    actions_for_identifier,
    export_bundle,
)
from tests.unit import enforcement_harness as h


@pytest.fixture(scope="module")
def conn():
    connection = h.connection()
    h.reviewed(connection)
    yield connection
    connection.close()


def keys(answer):
    return sorted(row["action_key"] for row in answer["actions"])


def test_entity_answers_use_accepted_matches_and_the_group_states_its_basis(conn):
    own = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert keys(own) == sorted([h.EDPB_IE, h.SEC_LR])
    group = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES, group=True)
    assert keys(group) == sorted([h.EDPB_IE, h.SEC_LR, h.SEC_AP, h.FCA_EXAMPLA, h.EPA])
    assert group["group_basis"]["accepted_ownership_identity_decisions"]
    epa = next(r for r in group["actions"] if r["action_key"] == h.EPA)
    assert epa["group_relation"] != "self" and epa["matched_through"]["low_evidence"] is True
    assert set(group["by_authority"]) == {"us-sec", "uk-fca", "us-epa", "eu-sa-ie"}
    assert forbidden_keys(group) == []


def test_outcomes_are_as_published_including_settlement_without_admission(conn):
    answer = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    sec = next(r for r in answer["actions"] if r["action_key"] == h.SEC_LR)
    assert sec["outcome"]["admission_wording"] == "Without admitting or denying the allegations in the complaint"
    assert sec["outcome"]["settled"] is True and "not a finding" in sec["outcome"]["note"]
    assert sec["cite"]["revision_id"].startswith("enf-rev:")
    assert all(p["cite"]["revision_id"] for p in sec["penalties"])
    assert {r.get("pseudonym") for r in sec["respondents"]} >= {"natural person 2"}
    assert "Jordan" not in str(answer)


def test_as_of_dates_show_only_what_was_published_by_then(conn):
    early = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                               group=True, as_of="2099-03-20")
    assert keys(early) == sorted([h.SEC_LR, h.FCA_EXAMPLA, h.EPA])
    assert {e["action_key"] for e in early["excluded"]} == {h.SEC_AP, h.EDPB_IE}
    epa = next(r for r in early["actions"] if r["action_key"] == h.EPA)
    assert epa["decisions"] == [] and epa["outcome"]["as_published"] is None
    assert "an initiated action is not a finding" in epa["outcome"]["note"]
    before = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                as_of="2090-01-01")
    assert before["status"] == "no_action_on_record" and "not a statement" in before["message"]


def test_appeal_history_and_unmatched_subjects():
    connection = h.connection()
    h.reviewed(connection)
    answer = actions_by_authority(connection, h.NS, scopes=h.SCOPES, authority="uk-fca")
    northwind = next(r for r in answer["actions"] if r["action_key"] == h.FCA_NORTHWIND)
    assert northwind["appeals"][0]["reference"] == "FS/2099/0007"
    assert northwind["appeal_status"] == "appeal published"
    early = action_history(connection, h.NS, h.FCA_NORTHWIND, scopes=h.SCOPES, as_of="2099-05-01")
    assert early["action"]["appeals"] == [] and early["revisions"][h.FCA_NORTHWIND]


def test_authority_answers_never_sum_penalties_and_keep_unknowns_explicit(conn):
    fca = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca", date_from="2099-01-01",
                               date_to="2099-12-31")
    assert keys(fca) == sorted([h.FCA_EXAMPLA, h.FCA_NORTHWIND])
    (listed,) = fca["penalties_by_authority_and_currency"]
    assert listed["currency"] == "GBP" and listed["count"] == 3 and "sum" not in listed
    assert "never summed" in fca["totals_note"] and forbidden_keys(fca) == []
    epa = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="us-epa")
    assert any(u.get("penalty_type") == "cost_recovery" for u in epa["unknowns"])
    edpb = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="eu-sa-nl")
    assert any(u.get("penalty_type") == "fine" for u in edpb["unknowns"])
    period = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca", date_from="2099-04-01")
    assert keys(period) == [h.FCA_NORTHWIND]
    with pytest.raises(EnforcementError):
        actions_by_authority(conn, h.NS, scopes=h.SCOPES)


def test_legal_basis_queries_match_exact_references(conn):
    gdpr = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="Article 6 (Lawfulness of processing)")
    assert keys(gdpr) == [h.EDPB_IE]
    exchange = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="us-sec",
                                    legal_basis="Securities Exchange Act of 1934")
    assert keys(exchange) == sorted([h.SEC_LR, h.SEC_AP])
    handbook = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="Principle 3")
    assert keys(handbook) == [h.FCA_EXAMPLA]


def test_published_identifier_lookup_needs_no_identity_match(conn):
    answer = actions_for_identifier(conn, h.NS, "fca-frn", "999777", scopes=h.READ_ONLY)
    assert keys(answer) == [h.FCA_NORTHWIND] and "not an identity match" in answer["note"]
    assert actions_for_identifier(conn, h.NS, "sec-cik", "9999101", scopes=h.READ_ONLY)["actions"]


def test_natural_persons_are_not_query_keys_and_ownership_absence_degrades(conn):
    with pytest.raises(EnforcementError) as caught:
        actions_for_entity(conn, h.NS, h.PERSON_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert caught.value.code == "natural_person_not_a_query_key"
    bare = h.connection()
    h.load_all(bare)
    answer = actions_for_entity(bare, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert answer["status"] == "ownership_unavailable"


def test_evidence_bundles_cite_every_revision_with_source_and_as_of(conn):
    answer = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                group=True, as_of="2099-12-31")
    bundle = export_bundle(answer)
    assert verify_bundle(bundle).valid
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    cited = {o["payload"]["citation"]["revision_id"] for o in evidence}
    assert {row["cite"]["revision_id"] for row in answer["actions"]} <= cited
    assert all(o["payload"]["as_of"] == "2099-12-31" and o["payload"]["citation"]["provider"] for o in evidence)
    empty = export_bundle(actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca",
                                               date_from="2100-01-01"))
    # An empty answer exports as an explicitly incomplete bundle with its omission, never as a clean result.
    assert verify_bundle(empty).status == "incomplete" and empty["completeness"]["omissions"]
