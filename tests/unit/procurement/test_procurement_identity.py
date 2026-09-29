"""Buyer/supplier identity links, award history and incumbency (P10)."""

import pytest

from src.kb.entities import add_manual_alias
from src.kb.entity_history import EntityHistoryStore
from src.kb.lei import LeiStore
from src.kb.procurement_identity import IdentityError, ProcurementIdentityService, party_key
from tests.unit.procurement.harness import BUYER, LEIS, NS, REVIEWER_SCOPES, SCOPES, Env

UK_SUPPLIER_LEI = LEIS["uk_digital"]


@pytest.fixture
def env():
    value = Env()
    value.acquire()
    LeiStore(value.conn, now=value.now).observe_page(NS, [{"lei_record": {
        "contract": "noesis-lei-part-v1", "provider": "gleif", "lei": UK_SUPPLIER_LEI, "part": "record", "raw_sha256": "f" * 64,
        "attributes": {"lei": UK_SUPPLIER_LEI, "entity": {"legalName": {"name": "Fixture Digital Ltd"}, "jurisdiction": "GB"},
                       "registration": {"status": "ISSUED", "lastUpdateDate": "2026-01-01T00:00:00Z"}}}}],
        run_id="fixture", page_receipt={})
    add_manual_alias(value.conn, "Nordlicht Fixture IT GmbH", "Nordlicht Fixture IT GmbH", "Organization")
    return value


def service(env):
    return ProcurementIdentityService(env.conn, now=env.now)


def supplier(env, name):
    return next(p["party"] for p in service(env).parties(NS, scopes=SCOPES) if p["party"]["name"] == name)


def test_parties_stay_source_strings_and_candidates_are_proposals_with_evidence(env):
    parties = service(env).parties(NS, scopes=SCOPES)
    names = {p["party"]["name"]: p for p in parties}
    assert {"buyer"} == set(names[BUYER]["roles"]) and names[BUYER]["link"] is None
    uk = supplier(env, "Fixture Digital Ltd")
    proposal = service(env).candidates(NS, uk, scopes=SCOPES | {"knowledge:companies:read"})
    lei = next(c for c in proposal["candidates"] if c["kind"] == "lei")
    assert lei["entity_id"] == f"lei:{UK_SUPPLIER_LEI}" and lei["evidence"]["lei_record"]["legal_name"] == "Fixture Digital Ltd"
    assert "proposals only" in proposal["policy"]
    nordlicht = supplier(env, "Nordlicht Fixture IT GmbH")
    alias = next(c for c in service(env).candidates(NS, nordlicht, scopes=SCOPES)["candidates"] if c["kind"] == "canonical_entity")
    assert alias["entity_id"].startswith("ent-") and service(env).link_for(NS, nordlicht) is None  # nothing applied automatically


def test_links_are_reviewed_auditable_reversible_decisions_and_never_merges(env):
    uk = supplier(env, "Fixture Digital Ltd")
    with pytest.raises(IdentityError):
        service(env).decide(NS, uk, f"lei:{UK_SUPPLIER_LEI}", decision="match", principal_id="alice", scopes=SCOPES)
    link = service(env).decide(NS, uk, f"lei:{UK_SUPPLIER_LEI}", decision="match", principal_id="reviewer", scopes=REVIEWER_SCOPES,
                               evidence={"lei": UK_SUPPLIER_LEI})
    assert link["merged"] is False and link["reversible"] and service(env).link_for(NS, uk)["entity_id"] == f"lei:{UK_SUPPLIER_LEI}"
    history = EntityHistoryStore(env.conn, initialize=False)
    assert history.resolve(NS, party_key(uk), scopes=REVIEWER_SCOPES)["canonical_id"] == party_key(uk)  # no redirect/merge
    decision = env.conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?", [link["decision_id"]]).fetchone()
    assert decision == ("match",)
    reverted = service(env).revert(NS, link["link_id"], principal_id="reviewer", scopes=REVIEWER_SCOPES)
    assert reverted["undoes"] == link["decision_id"] and service(env).link_for(NS, uk) is None
    assert [a["status"] for a in service(env).audit(NS, scopes=SCOPES)] == ["reverted"]


def test_award_history_is_queryable_per_buyer_supplier_cpv_and_linked_entity(env):
    svc = service(env)
    by_buyer = svc.award_history(NS, scopes=SCOPES, buyer=BUYER)
    assert [(a["notice_id"], a["date"]) for a in by_buyer] == [("00587654-2024", "2024-03-15")]
    assert all(a["source_url"].startswith("https://") for a in by_buyer)
    assert len(svc.award_history(NS, scopes=SCOPES, cpv="72222300")) == 1
    uk = supplier(env, "Fixture Digital Ltd")
    svc.decide(NS, uk, f"lei:{UK_SUPPLIER_LEI}", decision="match", principal_id="reviewer", scopes=REVIEWER_SCOPES)
    linked = svc.award_history(NS, scopes=SCOPES, supplier_entity=f"lei:{UK_SUPPLIER_LEI}")
    assert {a["stage"] for a in linked} == {"award", "modification"}


def test_incumbency_is_a_derived_explained_fact(env):
    result = service(env).incumbency(NS, scopes=SCOPES, buyer=BUYER, cpv=["72253000"], supplier_names=["Nordlicht Fixture IT GmbH"])
    assert result["incumbents"][0]["supplier"]["name"] == "Helpdesk Fixture Services GmbH"
    assert not result["incumbents"][0]["is_profile_supplier"] and "2024-03-15" in result["explanation"]
    assert "never evidence that a procedure is open" in result["semantics"]
    own = service(env).incumbency(NS, scopes=SCOPES, buyer="Stadt Fixturestadt", cpv=["72222300"],
                                  supplier_names=["Nordlicht Fixture IT GmbH"])
    assert own["incumbents"][0]["is_profile_supplier"]
    none = service(env).incumbency(NS, scopes=SCOPES, buyer=BUYER, cpv=["55520000"])
    assert none["incumbents"] == [] and "No award" in none["explanation"]
