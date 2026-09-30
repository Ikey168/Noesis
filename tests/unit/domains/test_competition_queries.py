"""Company-to-cases as of a date, stage history and state aid for a beneficiary (#2217, CS09, CS10)."""

from __future__ import annotations

import pytest

from src.kb.competition import CompetitionError, forbidden_keys
from src.kb.competition_queries import awards_for_beneficiary, case_history, cases_for_company
from tests.unit import competition_harness as h


@pytest.fixture(scope="module")
def conn():
    conn = h.connection()
    h.reviewed(conn)
    return conn


def ask(conn, entity, **kwargs):
    return cases_for_company(conn, h.NS, entity, ownership_namespace=h.OWN_NS, scopes=h.SCOPES, **kwargs)


def test_direct_cases_with_role_stage_as_of_and_the_identity_match(conn):
    answer = ask(conn, h.HOLD_ENTITY, as_of="2025-06-01")
    assert answer["status"] == "answered" and not forbidden_keys(answer)
    keys = {row["case_key"] for row in answer["cases"]}
    assert keys == {h.MERGER, h.CMA, h.FTC}
    merger = next(r for r in answer["cases"] if r["case_key"] == h.MERGER)
    assert merger["party"]["role_as_published"] == "Notifying party"
    assert merger["stage_as_of"]["stage_as_published"].startswith("Art. 6(1)(c)")  # the SO came on 2025-06-16
    assert merger["matched_through"]["method"] == "name-jurisdiction" and merger["matched_through"]["low_evidence"]
    assert merger["cites"]["case_revision_id"] and merger["cites"]["stage_revision_id"]
    assert merger["cites"]["identity_candidate"] and merger["cites"]["identity_decision"]
    assert set(answer["by_authority"]) == {"ec", "uk-cma", "us-ftc"}  # side by side, never merged
    later = ask(conn, h.HOLD_ENTITY, as_of="2025-07-01")
    assert next(r for r in later["cases"] if r["case_key"] == h.MERGER)["stage_as_of"]["stage_as_published"] == \
        "Statement of Objections"
    early = ask(conn, h.HOLD_ENTITY, as_of="2025-01-01")
    assert early["status"] == "no_case_on_record" and {e["case_key"] for e in early["excluded"]} == keys


def test_group_expansion_labels_the_member_and_ownership_path(conn):
    answer = ask(conn, h.HOLD_ENTITY, as_of="2025-06-01", group=True)
    rows = {r["case_key"]: r for r in answer["cases"]}
    assert h.AID_CASE in rows
    aid = rows[h.AID_CASE]
    assert aid["group_relation"] == "group-member" and aid["party"]["role"] == "beneficiary"
    assert aid["ownership_path"] and aid["ownership_path"][0]["assertion_ids"]
    assert {a["award_key"] for a in answer["awards_matched"]} == {h.AWARD_INT, h.AWARD_OTHER}
    # From the subsidiary, the group reaches the parent's cases through the stated parent chain.
    upward = ask(conn, h.INT_ENTITY, as_of="2025-06-01", group=True)
    assert h.MERGER in {r["case_key"] for r in upward["cases"]}
    direct = ask(conn, h.INT_ENTITY, as_of="2025-06-01")
    assert {r["case_key"] for r in direct["cases"]} == {h.AID_CASE}


def test_no_case_on_record_is_never_a_clean_bill_and_unknowns_are_listed(conn):
    answer = ask(conn, h.TRADE_ENTITY, as_of="2025-06-01", include_unknowns=True)
    assert answer["status"] == "no_case_on_record" and "not a clean bill" in answer["message"]
    assert answer["unknowns"] == []
    holding = ask(conn, h.HOLD_ENTITY, include_unknowns=True)
    assert all(u["status"] == "unmatched" for u in holding["unknowns"])
    with pytest.raises(CompetitionError):
        ask(conn, "gleif:lei:NOT-AN-ENTITY")


def test_stage_history_in_date_order_with_superseded_revisions():
    conn = h.connection()
    h.load_all(conn)
    h.load_all(conn, v2=True)
    history = case_history(conn, h.NS, h.MERGER, scopes=h.SCOPES, include_superseded=True)
    dates = [s["stage_date"] for s in history["stages"]]
    assert dates == sorted(dates) and history["stages"][-1]["stage_as_published"].startswith("Art. 8(2)")
    assert all(s["revision_id"] and s["source"]["provider"] == "ec-competition" for s in history["stages"])
    assert [r["state_as_published"] for r in history["case_revisions"]] == ["Open", "Closed"]
    assert {d["citation"].get("celex") for d in history["documents"]} >= {"32025M99001"}
    assert history["case"]["court_dockets"][0]["docket_number"] == "T-999/25"
    assert case_history(conn, h.NS, "competition:case:ec:M.00000", scopes=h.SCOPES)["status"] == "no_case_on_record"


def test_awards_for_a_beneficiary_with_computed_totals_and_explicit_unknowns(conn):
    answer = awards_for_beneficiary(conn, h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    awards = {a["award_key"]: a for a in answer["awards"]}
    assert set(awards) == {h.AWARD_INT, h.AWARD_OTHER}
    assert awards[h.AWARD_INT]["measure_case"] == {**awards[h.AWARD_INT]["measure_case"], "status": "resolved",
                                                   "case_key": h.AID_CASE}
    assert awards[h.AWARD_INT]["amount_as_published"] == "12500000.00" and awards[h.AWARD_INT]["currency"] == "EUR"
    assert awards[h.AWARD_INT]["granting_authority"].startswith("Rijksdienst")
    totals = answer["computed_totals"]
    assert "never stored" in totals["view"]
    assert totals["by_currency"] == [{"currency": "EUR", "sum": "13250000.00", "inputs": totals["by_currency"][0]["inputs"]}]
    assert len(totals["by_currency"][0]["inputs"]) == 2
    assert {"award_key": h.AWARD_OTHER, "unknown": "SA measure reference not resolved to an acquired case",
            "sa_number": "SA.99003"} in answer["unknowns"]
    none = awards_for_beneficiary(conn, h.NS, h.TRADE_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert none["status"] == "no_award_on_record"


def test_superseded_award_revisions_on_request():
    conn = h.connection()
    h.reviewed(conn)
    h.load_all(conn, v2=True)
    answer = awards_for_beneficiary(conn, h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                    include_superseded=True)
    award = next(a for a in answer["awards"] if a["award_key"] == h.AWARD_INT)
    assert [r["amount_as_published"] for r in award["revisions"]] == ["12500000.00", "12000000.00"]
    assert award["status"] == "corrected"
