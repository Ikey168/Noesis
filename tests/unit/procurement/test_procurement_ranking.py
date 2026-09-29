"""Explained shortlists: fit, value and effort with award history as context (P09)."""

import json

import jsonschema
import pytest

from src.kb.procurement_ranking import DEFAULT_WEIGHTS, SOURCES, RankingError, ShortlistService
from tests.unit.procurement.harness import NS, ROOT, SCOPES, TED_CANTEEN, TED_N1, UK_TENDER, Env, supplier_profile


@pytest.fixture
def built():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    service = ShortlistService(env.conn, now=env.now)
    shortlist = service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    return env, profile, service, shortlist


def item(env, shortlist, provider, procedure_id, lot):
    return next(i for i in shortlist["items"] if i["item_id"] == f"{env.key(provider, procedure_id)}#{lot}")


def test_every_ranking_input_is_listed_with_its_source_and_fit_is_not_a_probability(built):
    env, _, _, shortlist = built
    jsonschema.validate(shortlist, json.loads((ROOT / "contracts/schemas/jsonschema/noesis-procurement-shortlist-v1.json").read_text()))
    assert "not a probability" in shortlist["score_semantics"]
    lot1 = item(env, shortlist, "ted", TED_N1, "LOT-0001")
    assert set(lot1["criteria"]) == set(DEFAULT_WEIGHTS) == set(SOURCES)
    assert all(c["source"] and c["reasons"] for c in lot1["criteria"].values())
    assert lot1["bucket"] == "apply_now" and lot1["verdict"] == "eligible"
    assert lot1["criteria"]["cpv_fit"]["score"] == 1.0 and lot1["criteria"]["jurisdiction"]["score"] == 1.0
    assert "estimated 400000 EUR (VAT excluded; estimated, not awarded)" in lot1["criteria"]["value_fit"]["reasons"]
    assert any("2026-11-03+01:00 12:00:00+01:00" in r for r in lot1["criteria"]["deadline_feasibility"]["reasons"])
    assert "Helpdesk Fixture Services GmbH" in lot1["criteria"]["incumbency_context"]["reasons"][0]
    assert 0 <= lot1["match_score"] <= 1 and lot1["score_coverage"] == 1.0


def test_past_awards_are_context_and_never_evidence_that_a_procedure_is_open(built):
    env, _, _, shortlist = built
    lot1 = item(env, shortlist, "ted", TED_N1, "LOT-0001")
    assert lot1["award_context"] and lot1["award_semantics"].startswith("past awards are context only")
    assert {a["relation"] for a in lot1["award_context"]} >= {"same buyer and CPV"}
    award_procedures = {a["procedure_key"] for a in env.notices().award_history(NS, scopes=SCOPES)}
    assert not any(i["procedure_key"] in award_procedures for i in shortlist["items"])  # awards are never shortlisted


def test_ineligible_closed_and_off_profile_items_are_never_apply_now(built):
    env, _, _, shortlist = built
    assert item(env, shortlist, "ted", TED_N1, "LOT-0002")["bucket"] == "excluded"
    canteen = item(env, shortlist, "ted", TED_CANTEEN, "LOT-0001")
    assert canteen["criteria"]["cpv_fit"]["score"] == 0.0 and canteen["bucket"] == "consider"
    closed = [i for i in shortlist["items"] if i["state"] == "closed"]
    assert closed and all(i["bucket"] == "excluded" for i in closed)
    watch = [i for i in shortlist["items"] if i["state"] == "forthcoming"]
    assert watch and all(i["bucket"] == "watch" for i in watch)
    uk2 = item(env, shortlist, "uk-fts", UK_TENDER, "2")
    assert uk2["bucket"] == "excluded"  # misses Cyber Essentials Plus on stated facts


def test_shortlist_replays_exactly_from_pinned_revisions_even_after_corrigenda(built):
    env, _, service, shortlist = built
    assert service.replay(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)["identical"]
    env.acquire(2)
    inspected = service.inspect(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)
    assert not inspected["freshness"]["current"]
    replay = service.replay(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)
    assert replay["identical"] and not replay["identity_links_changed"]


def test_sensitivity_and_weights(built):
    env, profile, service, shortlist = built
    assert set(shortlist["sensitivity"]) == {f"{c}:{m}" for c in DEFAULT_WEIGHTS for m in ("dropped", "doubled")}
    with pytest.raises(RankingError):
        service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES, weights={"win_probability": 1})
    reweighted = service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES, weights={"incumbency_context": 0})
    assert reweighted["weights"]["incumbency_context"] == 0


def test_shortlists_are_owner_scoped(built):
    env, _, service, shortlist = built
    with pytest.raises(RankingError):
        service.inspect(NS, shortlist["shortlist_id"], principal_id="mallory", scopes=SCOPES)
