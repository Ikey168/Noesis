"""Actions for an entity or its group as of a date, and by authority and legal basis (#2651, EN09, EN10)."""

from __future__ import annotations

import json

import pytest

from src.kb.enforcement import EnforcementError, forbidden_keys
from src.kb.enforcement_queries import (
    action_history,
    actions_by_authority,
    actions_for_entity,
)
from tests.unit import enforcement_harness as h


@pytest.fixture(scope="module")
def conn():
    connection = h.connection()
    h.reviewed(connection)
    yield connection
    connection.close()


def entity(conn, key, **kwargs):
    return actions_for_entity(conn, h.NS, key, ownership_namespace=h.OWN_NS, scopes=h.SCOPES, **kwargs)


def test_group_expansion_uses_accepted_matches_and_states_them(conn):
    alone = entity(conn, h.HOLD_ENTITY)
    assert [r["action_key"] for r in alone["actions"]] == [h.SEC_LR]
    group = entity(conn, h.HOLD_ENTITY, group=True)
    assert {r["action_key"] for r in group["actions"]} == {h.SEC_LR, h.FCA_EX, h.EDPB_EX}
    assert "accepted" in group["group_basis"] and group["matches_used"]
    assert all(m["candidate_id"] and m["decision_id"] for m in group["matches_used"])
    fca = next(r for r in group["actions"] if r["action_key"] == h.FCA_EX)
    assert fca["group_relation"] == "group-member" and fca["ownership_path"]
    assert fca["matched_through"]["low_evidence"] is True  # a reviewed name match, marked as such
    assert group["by_authority"] == {"eu-dpa-nl": [h.EDPB_EX], "uk-fca": [h.FCA_EX], "us-sec": [h.SEC_LR]}


def test_outcomes_are_shown_as_published_including_settled_without_admission(conn):
    row = entity(conn, h.HOLD_ENTITY)["actions"][0]
    assert row["settlement_as_published"] == {
        "settled_as_published": True,
        "admission_as_published": "Without admitting or denying the allegations in the complaint"}
    assert "consented to the entry of a final judgment" in row["outcome_as_published"]
    assert [(p["amount_as_published"], p["currency"]) for p in row["penalties"]] == [("$2,500,000", "USD")]
    text = json.dumps(row).lower()
    for word in ("liable", "guilty", "found to have", "risk"):
        assert word not in text
    assert forbidden_keys(row) == []


def test_answers_cite_each_revision_and_answer_as_of_a_date(conn):
    row = entity(conn, h.HOLD_ENTITY)["actions"][0]
    cites = row["cites"]
    assert cites["action_revision_id"] == row["action_revision"]["revision_id"]
    assert cites["respondent_revision_id"] and cites["penalty_revision_ids"] and cites["notice_revision_ids"]
    assert cites["identity_candidate"] and cites["identity_decision"]
    before = entity(conn, h.HOLD_ENTITY, group=True, as_of="2025-05-01")
    assert [r["action_key"] for r in before["actions"]] == [h.FCA_EX]
    assert {e["action_key"] for e in before["excluded"]} == {h.SEC_LR, h.EDPB_EX}
    assert before["actions"][0]["outcome_at_date"] == "decided by the date"


def test_appeal_history_at_a_date_and_no_action_on_record_is_not_a_clean_bill(conn):
    rows = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca")["actions"]
    northwind = next(r for r in rows if r["action_key"] == h.FCA_NW)
    appeal = northwind["appeals"][0]
    assert (appeal["forum_as_published"], appeal["reference"], appeal["decided_on"]) == (
        "Upper Tribunal", "FS/2023/0017", "2024-09-30")
    nothing = entity(conn, h.UK_ENTITY, as_of="2024-01-01")
    assert nothing["status"] == "no_action_on_record" and "not a clean bill" in nothing["message"]
    assert "not a statement that no action exists" in nothing["coverage"]
    with pytest.raises(EnforcementError):
        entity(conn, "gleif:lei:NOTANENTITY000000000")


def test_by_authority_and_legal_basis_over_a_period_never_sums_penalties(conn):
    epa = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="us-epa")
    assert {r["action_key"] for r in epa["actions"]} == {h.EPA_EX, h.EPA_NW}
    figures = epa["penalty_figures"]["by_authority_and_currency"]["us-epa"]["USD"]
    assert {f["amount_as_published"] for f in figures} == {"$42,000", "$0"}
    assert "never summed" in epa["penalty_figures"]["note"]
    assert "sum" not in json.dumps(epa["penalty_figures"]["by_authority_and_currency"])
    undisclosed = {(u["action_key"], u["penalty_type"]) for u in epa["undisclosed_penalties"]}
    assert (h.EPA_EX, "federal_penalty") in undisclosed and (h.EPA_NW, "state_local_penalty") in undisclosed
    assert epa["cites"] == [r["action_revision"]["revision_id"] for r in epa["actions"]]
    window = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="us-epa", date_from="2025-01-01",
                                  date_to="2025-12-31")
    assert [(r["action_key"], r["period_date_field"]) for r in window["actions"]] == [
        (h.EPA_NW, "decided_on"), (h.EPA_EX, "initiated_on")]
    gdpr = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="Article 32 GDPR")
    assert [r["action_key"] for r in gdpr["actions"]] == [h.EDPB_EX]
    whole = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="Regulation (EU) 2016/679")
    assert {r["action_key"] for r in whole["actions"]} == {h.EDPB_EX, h.EDPB_ANON}
    exchange = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="15 U.S.C. § 78j")
    assert [r["action_key"] for r in exchange["actions"]] == [h.SEC_LR]
    lead = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="eu-dpa-ie")
    assert [r["lead_authority"]["name_as_published"] for r in lead["actions"]] == [
        "Irish Supervisory Authority (Data Protection Commission)"]
    mixed = actions_by_authority(conn, h.NS, scopes=h.SCOPES, legal_basis="section 206 of the Act")
    currencies = mixed["penalty_figures"]["by_authority_and_currency"]
    assert set(currencies) == {"uk-fca"} and set(currencies["uk-fca"]) == {"GBP"}
    with pytest.raises(EnforcementError):
        actions_by_authority(conn, h.NS, scopes=h.SCOPES)
    with pytest.raises(EnforcementError):
        actions_by_authority(conn, h.NS, scopes=set(), authority="us-epa")


def test_history_keeps_corrections_removals_and_superseded_revisions():
    conn = h.connection()
    h.load_all(conn)
    h.load_all(conn, v2=True)
    history = action_history(conn, h.NS, h.FCA_EX, scopes=h.SCOPES)
    assert [r["source_status"] for r in history["action_revisions"]] == ["published", "corrected"]
    notice = next(iter(history["notice_revisions"].values()))
    assert [r["record"]["amended_on"] for r in notice] == [None, "2025-05-02"]
    removed = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="eu-dpa-ie")
    assert removed["actions"] == [] and removed["removed_by_source"][0]["action_key"] == h.EDPB_ANON
    kept = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="eu-dpa-ie", include_removed=True)
    assert kept["actions"][0]["source_status"] == "removed_by_source"
    epa = action_history(conn, h.NS, h.EPA_EX, scopes=h.SCOPES)
    assert [r["record"]["status_as_published"] for r in epa["action_revisions"]] == ["Active", "Closed"]
    assert action_history(conn, h.NS, "enforcement:action:us-sec:LR-00000", scopes=h.SCOPES)["status"] == \
        "no_action_on_record"
