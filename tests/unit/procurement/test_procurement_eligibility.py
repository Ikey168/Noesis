"""Per-lot eligibility with cited passages, unknowns and pinned revisions (P08)."""

import pytest

from src.kb.procurement_eligibility import ProcurementEligibilityError, ProcurementEligibilityService, assess_lot, canonical_certification
from src.kb.procurement_profiles import ProcurementProfileStore
from tests.unit.procurement.harness import NS, SAM_SOLICITATION, SCOPES, TED_N1, UK_TENDER, Env, supplier_profile


@pytest.fixture
def env():
    value = Env()
    value.acquire()
    return value


def service(env):
    return ProcurementEligibilityService(env.conn, now=env.now)


def test_each_conclusion_cites_the_notice_revision_passage_and_profile_fact(env):
    profile = supplier_profile(env)
    result = service(env).assess(NS, profile["profile_id"], env.key("ted", TED_N1), principal_id="alice", scopes=SCOPES)
    assert result["verdicts"] == {"LOT-0001": "eligible", "LOT-0002": "ineligible"}
    turnover = next(f for f in result["lots"]["LOT-0001"]["findings"] if f["requirement_id"] == "ted:LOT-0001:criterion:1")
    assert turnover["result"] == "met" and turnover["facts_used"] == ["supplier.annual_turnover.EUR"]
    assert turnover["profile_facts"] == {"supplier.annual_turnover.EUR": {"amount": "1500000", "currency": "EUR"}}
    citation = turnover["citation"]
    assert citation["notice_revision"] == 1 and citation["notice_id"] == "00612345-2026" and citation["lot_id"] == "LOT-0001"
    assert citation["quote"].startswith("Minimum annual turnover of EUR 800 000") and citation["locator"]["field"] == "BT-750-Lot"
    assert result["lots"]["LOT-0002"]["disqualifiers"] == ["ted:LOT-0002:criterion:1", "ted:LOT-0002:criterion:2"]
    assert "not the contracting authority's decision" in result["disclaimer"]


def test_missing_facts_and_unparsed_criteria_are_unknown_never_a_pass(env):
    profile = supplier_profile(env, drop=("exclusion.tax_arrears", "supplier.references_count"))
    result = service(env).assess(NS, profile["profile_id"], env.key("ted", TED_N1), principal_id="alice", scopes=SCOPES)
    lot1 = result["lots"]["LOT-0001"]
    assert lot1["verdict"] == "needs_clarification"
    assert {"exclusion.tax_arrears", "supplier.references_count"} <= set(lot1["missing_facts"])
    assert {f["requirement_id"]: f["result"] for f in lot1["findings"]}["ted:exclusion:tax-pay"] == "unknown"
    clearance = next(f for f in result["lots"]["LOT-0002"]["findings"] if f["requirement_id"] == "ted:LOT-0002:criterion:3")
    assert clearance["result"] == "unparsed"


def test_money_thresholds_never_compare_across_currencies(env):
    profile = supplier_profile(env)  # EUR turnover
    result = service(env).assess(NS, profile["profile_id"], env.key("uk-fts", UK_TENDER), principal_id="alice", scopes=SCOPES)
    finding = next(f for f in result["lots"]["1"]["findings"] if f["requirement_id"] == "uk-fts:criterion:1")
    assert finding["result"] == "unknown" and finding["missing_facts"] == ["supplier.annual_turnover.GBP"]


def test_set_aside_and_certificates_are_rules_over_stated_facts(env):
    profile = supplier_profile(env, overrides={"supplier.set_aside_statuses": {"value": ["small-business"]},
                                               "supplier.certifications": {"value": ["iso/iec 20000-1:2018", "ISO 9001"]}})
    ted = service(env).assess(NS, profile["profile_id"], env.key("ted", TED_N1), principal_id="alice", scopes=SCOPES)
    cert = next(f for f in ted["lots"]["LOT-0001"]["findings"] if f["requirement_id"] == "ted:LOT-0001:criterion:3")
    assert cert["result"] == "met"
    sam = service(env).assess(NS, profile["profile_id"], env.key("sam-gov", SAM_SOLICITATION), principal_id="alice", scopes=SCOPES)
    assert sam["verdicts"] == {"_": "eligible"}
    assert canonical_certification("ISO/IEC 27001:2022") == "ISO/IEC 27001" and canonical_certification("Cyber Essentials") == "Cyber Essentials"


def test_reevaluation_is_triggered_by_notice_and_profile_revisions_and_pins_both(env):
    profile = supplier_profile(env)
    svc = service(env)
    key = env.key("ted", TED_N1)
    first = svc.assess(NS, profile["profile_id"], key, principal_id="alice", scopes=SCOPES)
    assert first["procedure"]["revision"] == 1 and first["profile"]["revision"] == 2
    assert svc.stale_assessments(NS, principal_id="alice", scopes=SCOPES) == []
    env.acquire(2)
    stale = svc.stale_assessments(NS, principal_id="alice", scopes=SCOPES)
    assert stale[0]["reasons"] == ["notice revision 1 -> 2"]
    redone = svc.reassess(NS, principal_id="alice", scopes=SCOPES)
    fresh = svc.inspect(NS, redone[0]["reassessed"], principal_id="alice", scopes=SCOPES)
    assert fresh["procedure"]["revision"] == 2 and fresh["verdicts"]["LOT-0001"] == "needs_clarification"  # "or equivalent" is unparsed
    ProcurementProfileStore(env.conn, now=env.now).update(NS, profile["profile_id"], "turnover", 2, principal_id="alice", scopes=SCOPES,
                                                          set_facts={"supplier.annual_turnover": {"value": {"amount": "2500000", "currency": "EUR"}}})
    assert svc.stale_assessments(NS, principal_id="alice", scopes=SCOPES)[0]["reasons"] == ["profile revision 2 -> 3"]
    pinned = svc.assess(NS, profile["profile_id"], key, principal_id="alice", scopes=SCOPES, profile_revision=2, notice_revision=1)
    assert pinned["assessment_id"] == first["assessment_id"]  # replay from pins is identical


def test_only_the_owner_can_assess_and_buyer_profiles_are_refused(env):
    profile = supplier_profile(env)
    with pytest.raises(Exception):
        service(env).assess(NS, profile["profile_id"], env.key("ted", TED_N1), principal_id="mallory", scopes=SCOPES)
    buyer = ProcurementProfileStore(env.conn, now=env.now).create(NS, "b", label="Buyer", principal_id="alice", scopes=SCOPES, kind="buyer")
    with pytest.raises(ProcurementEligibilityError):
        service(env).assess(NS, buyer["profile_id"], env.key("ted", TED_N1), principal_id="alice", scopes=SCOPES)


def test_a_lot_without_criteria_needs_clarification(env):
    procedure = env.notices().get(NS, env.key("uk-cf", "ocds-b5fd17-fixture-cf-001"), scopes=SCOPES)
    result = assess_lot(procedure, None, {})
    assert result["verdict"] == "needs_clarification"
    assert "states no exclusion grounds or selection criteria" in result["clarifications"][0]["reason"]
