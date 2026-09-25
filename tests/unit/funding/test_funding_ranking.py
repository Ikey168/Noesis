"""Explainable ranking by fit, usable support and effort (#1771)."""

import json
from pathlib import Path

import jsonschema
import pytest

from src.kb.funding_ranking import RankingError, ShortlistService, score_opportunity
from src.kb.funding_records import record
from tests.unit.funding.harness import NS, SCOPES, Env, founder_profile, ms

ROOT = Path(__file__).resolve().parents[3]


def _built(env, **kwargs):
    env.acquire_all()
    profile = founder_profile(env)
    return profile, ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES, **kwargs)


def _item(shortlist, provider_id_part):
    return next(i for i in shortlist["items"] if provider_id_part in i["title"])


def test_buckets_keep_eligibility_separate_from_fit():
    env = Env()
    _, shortlist = _built(env)
    jsonschema.validate(shortlist, json.loads((ROOT / "contracts/schemas/jsonschema/noesis-funding-shortlist-v1.json").read_text()))
    assert "not a probability" in shortlist["score_semantics"]
    for item in shortlist["items"]:
        if item["bucket"] == "apply_now":
            assert item["verdict"] == "eligible" and item["state"] in {"open", "rolling"}
        if item["verdict"] == "ineligible" or item["state"] == "closed":
            assert item["bucket"] == "excluded"
    assert _item(shortlist, "EXIST Business Start-up")["bucket"] == "apply_now"
    assert _item(shortlist, "Quantum sensing")["bucket"] == "excluded"
    # High topic fit does not override unresolved eligibility.
    eu = _item(shortlist, "Open-source internet infrastructure")
    assert eu["bucket"] == "consider" and eu["criteria"]["topic_fit"]["score"] > 0


def test_loans_are_not_cash_and_budgets_are_not_award_sizes():
    env = Env()
    _, shortlist = _built(env)
    loan = _item(shortlist, "StartGeld")
    assert loan["criteria"]["usable_funding"]["score"] == 0.0
    assert "not a cash grant" in loan["criteria"]["usable_funding"]["reasons"][0]
    assert loan["bucket"] == "watch"  # a directory listing is never apply-now
    eu = _item(shortlist, "Open-source internet infrastructure")
    assert "per project" in eu["criteria"]["usable_funding"]["reasons"][0]
    assert "30000000" not in json.dumps(eu["criteria"]["usable_funding"])
    stipend = _item(shortlist, "EXIST Business Start-up")
    assert "per person-month" in stipend["criteria"]["usable_funding"]["reasons"][0]


def test_unknowns_freshness_reasons_and_sensitivity_are_exposed():
    env = Env()
    _, shortlist = _built(env)
    item = _item(shortlist, "EXIST Business Start-up")
    assert "cash_flow" in item["unknown_criteria"] and item["score_coverage"] < 1
    assert item["source_freshness"]["last_execution"] == "injected"
    assert all(set(c) == {"score", "reasons", "weight"} for c in item["criteria"].values())
    assert set(shortlist["sensitivity"]) == {f"{c}:{m}" for c in item["criteria"] for m in ("dropped", "doubled")}
    assert "applicant.sme" in shortlist["unknown_profile_facts"]


def test_priorities_change_order_but_never_bucket_rules():
    env = Env()
    _, default = _built(env)
    weighted = ShortlistService(env.conn, now=env.now).build(
        NS, default["profile_id"], principal_id="alice", scopes=SCOPES,
        weights={"topic_fit": 0, "usable_funding": 0, "deadline_feasibility": 0, "application_effort": 10})
    assert [i["bucket"] for i in weighted["items"]] == sorted(
        (i["bucket"] for i in weighted["items"]), key=["apply_now", "consider", "watch", "excluded"].index)
    assert {i["opportunity_id"]: i["bucket"] for i in weighted["items"]} == {i["opportunity_id"]: i["bucket"] for i in default["items"]}
    with pytest.raises(RankingError):
        ShortlistService(env.conn, now=env.now).build(NS, default["profile_id"], principal_id="alice", scopes=SCOPES,
                                                      weights={"luck": 1})


def test_expired_and_infeasible_deadlines_are_not_apply_now():
    rec = record("nlnet", "call", "f", "Fund", source_url="https://nlnet.nl/propose/", authority={"kind": "funder", "name": "NLnet"},
                 instrument={"kinds": ["grant"]}, status={"asserted": "open"},
                 deadlines=[{"kind": "submission", "text": "soon", "instant": "2026-09-27T12:00:00+02:00", "timezone": "Europe/Amsterdam"}])
    from src.kb.funding_opportunities import effective_status
    now = ms("2026-09-25T09:00:00+00:00")
    opportunity = {"opportunity_id": "o", "revision": 1, "record": rec, "status": effective_status(rec, as_of_ms=now)}
    eligible = {"verdict": "eligible", "disqualifiers": [], "clarifications": []}
    tight = score_opportunity(opportunity, eligible, {"preferences.min_days_to_deadline": 10}, as_of_ms=now)
    assert tight["bucket"] == "consider" and tight["criteria"]["deadline_feasibility"]["score"] == 0.0
    expired = {**opportunity, "status": effective_status(rec, as_of_ms=ms("2026-10-01T00:00:00+00:00"))}
    assert score_opportunity(expired, eligible, {}, as_of_ms=ms("2026-10-01T00:00:00+00:00"))["bucket"] == "excluded"


def test_shortlists_are_owner_scoped_and_invalidated_by_amendments():
    env = Env()
    profile, shortlist = _built(env)
    service = ShortlistService(env.conn, now=env.now)
    with pytest.raises(RankingError):
        service.inspect(NS, shortlist["shortlist_id"], principal_id="mallory", scopes=SCOPES)
    assert service.inspect(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)["freshness"]["current"]
    env.web.routes["https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json"] = "eu_topic_open_amended.json"
    env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="amend")
    freshness = service.inspect(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)["freshness"]
    assert not freshness["current"] and "deadline_shift" in freshness["invalidations"][0]["reasons"]
