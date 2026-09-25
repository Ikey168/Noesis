"""Eligibility with cited rules and unknowns (#1770)."""

import pytest

from src.kb.funding_eligibility import (
    DISCLAIMER,
    EligibilityError,
    EligibilityService,
    InterpretationStore,
    assess,
    evaluate_rule,
)
from src.kb.funding_opportunities import FundingOpportunityStore
from src.kb.funding_records import record
from tests.unit.funding.harness import NS, REVIEWER_SCOPES, SCOPES, Env, founder_profile

LOC = {"quote": "fixture"}


def _opportunity(requirements, conflicts=None):
    rec = record("eu-ft", "call", "T", "Topic", source_url="https://ec.europa.eu/t", authority={"kind": "funder", "name": "EC"},
                 requirements=requirements, status={"asserted": "open"})
    return {"opportunity_id": "funding-opportunity:t", "revision": 3, "record": rec, "conflicts": conflicts or []}


def _req(rid, category, rule=None, hard=True):
    item = {"requirement_id": rid, "category": category, "text": rid + " text", "hard": hard, "locator": LOC}
    if rule:
        item["machine_rule"] = rule
    return item


def test_three_valued_and_or_and_exceptions():
    residence = {"fact": "applicant.residence_country", "op": "in", "value": ["DE", "AT"]}
    incorporated = {"fact": "applicant.establishment_country", "op": "in", "value": ["DE", "AT"]}
    either = {"any": [residence, incorporated]}
    assert evaluate_rule(either, {"applicant.residence_country": "FR", "applicant.establishment_country": "DE"})[0] is True
    assert evaluate_rule(either, {"applicant.residence_country": "FR"})[0] is None
    assert evaluate_rule(either, {"applicant.residence_country": "DE"})[0] is True
    both = {"all": [residence, incorporated]}
    assert evaluate_rule(both, {"applicant.residence_country": "FR"})[0] is False
    exception = {"rule": {"fact": "applicant.incorporated", "op": "is_false"},
                 "except": {"fact": "applicant.company_age_months", "op": "lte", "value": 12}}
    assert evaluate_rule(exception, {"applicant.incorporated": True, "applicant.company_age_months": 6})[0] is True
    assert evaluate_rule(exception, {"applicant.incorporated": True, "applicant.company_age_months": 30})[0] is False
    assert evaluate_rule({"not": residence}, {})[0] is None


def test_verdicts_cite_rules_and_report_fact_dependencies():
    opportunity = _opportunity([
        _req("uni", "affiliation", {"fact": "applicant.university_affiliation", "op": "is_true"}),
        _req("lic", "licensing", {"fact": "project.open_source", "op": "is_true"}),
        _req("stage", "project-stage", {"fact": "project.stage", "op": "in", "value": ["idea", "prototype"]}),
        _req("route", "submission-route"),
    ])
    facts = {"applicant.university_affiliation": True, "project.open_source": True, "project.stage": "prototype"}
    result = assess(opportunity, facts)
    assert result["verdict"] == "eligible" and result["disclaimer"] == DISCLAIMER
    cited = next(f for f in result["findings"] if f["requirement_id"] == "uni")
    assert cited["citation"] == {"opportunity_id": "funding-opportunity:t", "revision": 3, "source_url": "https://ec.europa.eu/t",
                                 "requirement_id": "uni", "locator": LOC}
    assert cited["facts_used"] == ["applicant.university_affiliation"]
    assert next(f for f in result["findings"] if f["requirement_id"] == "route")["result"] == "procedural"
    missing = assess(opportunity, {"project.open_source": True, "project.stage": "prototype"})
    assert missing["verdict"] == "needs_clarification" and missing["missing_facts"] == ["applicant.university_affiliation"]
    failing = assess(opportunity, {**facts, "project.open_source": False})
    assert failing["verdict"] == "ineligible" and failing["disqualifiers"] == ["lic"]


def test_unparsed_unclear_hardness_and_conflicts_need_clarification():
    unparsed = assess(_opportunity([_req("consortium", "consortium")]), {"applicant.kind": "consortium"})
    assert unparsed["verdict"] == "needs_clarification" and unparsed["clarifications"][0]["result"] == "unparsed"
    unclear = assess(_opportunity([_req("uni", "affiliation", {"fact": "applicant.university_affiliation", "op": "is_true"}, hard=None)]),
                     {"applicant.university_affiliation": False})
    assert unclear["verdict"] == "needs_clarification"
    conflict = assess(_opportunity([], conflicts=[{"field": "requirements", "values": [
        {"source_url": "https://a.example"}, {"source_url": "https://b.example"}]}]), {})
    assert conflict["verdict"] == "needs_clarification"
    assert assess(_opportunity([]), {})["verdict"] == "needs_clarification"


@pytest.mark.parametrize(("facts", "verdict"), [
    ({"applicant.residence_country": "DE", "applicant.establishment_country": "NL"}, "eligible"),
    # Establishment in DE never substitutes for residence in DE.
    ({"applicant.residence_country": "NL", "applicant.establishment_country": "DE"}, "ineligible"),
    ({"applicant.establishment_country": "DE"}, "needs_clarification"),
])
def test_residence_versus_incorporation_are_different_facts(facts, verdict):
    opportunity = _opportunity([_req("res", "residence", {"fact": "applicant.residence_country", "op": "eq", "value": "DE"})])
    assert assess(opportunity, facts)["verdict"] == verdict


def test_co_financing_and_consortium_rules():
    co = _opportunity([_req("co", "co-financing", {"fact": "project.matching_funds_available", "op": "gte", "value": {"amount": "10000", "currency": "EUR"}})])
    assert assess(co, {"project.matching_funds_available": {"amount": "12000", "currency": "EUR"}})["verdict"] == "eligible"
    assert assess(co, {"project.matching_funds_available": {"amount": "5000", "currency": "EUR"}})["verdict"] == "ineligible"
    consortium = _opportunity([_req("c", "consortium", {"all": [
        {"fact": "applicant.kind", "op": "eq", "value": "consortium"},
        {"fact": "project.consortium_partner_countries", "op": "intersects", "value": ["DE", "FR"]}]})])
    assert assess(consortium, {"applicant.kind": "informal-team"})["verdict"] == "ineligible"


def test_human_interpretations_are_reviewed_versioned_and_bound_to_requirement_text():
    env = Env()
    env.acquire("eu-ft", "eu_search_all", observation="s", text="fixture", page_size=2)
    env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="t")
    profile = founder_profile(env)
    store = FundingOpportunityStore(env.conn, now=env.now)
    topic = next(o for o in store.list(NS, scopes=SCOPES) if o["record"]["provider_id"] == "HORIZON-FIXTURE-2026-01-01")
    consortium = next(r for r in topic["record"]["requirements"] if "Consortium composition" in r["text"])
    service = EligibilityService(env.conn, now=env.now)
    before = service.assess(NS, profile["profile_id"], topic["opportunity_id"], principal_id="alice", scopes=SCOPES)
    assert before["verdict"] == "needs_clarification"
    interpretations = InterpretationStore(env.conn, now=env.now)
    rule = {"fact": "applicant.kind", "op": "eq", "value": "consortium"}
    proposed = interpretations.propose(NS, topic, consortium["requirement_id"], rule, note="3 entities => consortium applicant",
                                       principal_id="alice", scopes=SCOPES)
    with pytest.raises(EligibilityError):
        interpretations.review(NS, proposed["interpretation_id"], 1, approve=True, principal_id="alice", scopes=REVIEWER_SCOPES)
    interpretations.review(NS, proposed["interpretation_id"], 1, approve=True, principal_id="bob", scopes=REVIEWER_SCOPES)
    after = service.assess(NS, profile["profile_id"], topic["opportunity_id"], principal_id="alice", scopes=SCOPES)
    assert after["verdict"] == "ineligible" and after["interpretations"] == [proposed["interpretation_id"] + "@1"]
    source = next(f for f in after["findings"] if f["requirement_id"] == consortium["requirement_id"])["rule_source"]
    assert source.startswith("human-interpretation:")
    # An amendment of the requirement text detaches the interpretation.
    changed = {**topic, "record": {**topic["record"], "requirements": [
        {**r, "text": r["text"] + " (amended)"} if r["requirement_id"] == consortium["requirement_id"] else r
        for r in topic["record"]["requirements"]]}}
    assert interpretations.applicable(NS, changed) == {}


def test_assessments_pin_profile_and_call_revisions():
    env = Env()
    env.acquire("exist", "exist_programme", "https://www.exist.de/EXIST/Navigation/EN/Start-up-grant/start-up-grant.html", observation="e")
    profile = founder_profile(env)
    service = EligibilityService(env.conn, now=env.now)
    exist = service.opportunities.list(NS, scopes=SCOPES)[0]
    result = service.assess(NS, profile["profile_id"], exist["opportunity_id"], principal_id="alice", scopes=SCOPES)
    assert result["verdict"] == "eligible"
    assert result["profile"]["revision"] == 2 and result["opportunity"]["revision"] == 1
    older = service.assess(NS, profile["profile_id"], exist["opportunity_id"], principal_id="alice", scopes=SCOPES, profile_revision=1)
    assert older["verdict"] == "needs_clarification" and older["assessment_id"] != result["assessment_id"]
    with pytest.raises(Exception):
        service.assess(NS, profile["profile_id"], exist["opportunity_id"], principal_id="mallory", scopes=SCOPES)
