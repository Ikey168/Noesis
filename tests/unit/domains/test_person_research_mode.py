"""Person-research mode (``NOESIS_PERSON_RESEARCH``): natural-person limits lift only when the switch is on.

The default (switch off) behaviour stays covered by each module's own tests; these
tests pin what changes when an operator turns the mode on for private research.
"""

from __future__ import annotations

import copy

import duckdb
import pytest

from src.config.env import person_research_enabled
from src.ingestion.campaign_finance_sources import (
    PERSON_RESEARCH_POLICY,
    fec_counterparty,
    minimisation_violations,
)
from src.osint import movements

ROW = {
    "entity_type": "IND", "contributor_name": "EXAMPLE, PAT", "contributor_employer": "FICTIONAL HOLDINGS",
    "contributor_occupation": "INVESTOR", "contributor_city": "SPRINGFIELD", "contributor_state": "ZZ",
    "contributor_street_1": "1 FICTIONAL WAY", "contributor_zip": "00000", "contributor_id": "c00000001",
}


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)


def test_the_switch_is_off_by_default_and_reads_the_canonical_name(off, monkeypatch):
    assert person_research_enabled() is False
    for value, expected in (("on", True), ("true", True), ("YES", True), ("0", False), ("off", False)):
        monkeypatch.setenv("NOESIS_PERSON_RESEARCH", value)
        assert person_research_enabled() is expected


def _registry(natural: bool) -> dict:
    return {"event": "registered", "statement": {"as_published": {
        "registrant": {"kind": "individual", "name": "Pat Example", "natural_person": natural}}}}


def test_movements_answer_privately_registered_aircraft_only_in_research_mode(off, monkeypatch):
    assert movements.natural_person_registrant(_registry(True)) is True
    assert movements._withhold(_registry(True)["statement"]["as_published"])["registrant"]["name"].startswith(
        "withheld")
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    assert movements.natural_person_registrant(_registry(True)) is False
    assert movements._withhold(_registry(True)["statement"]["as_published"])["registrant"]["name"] == "Pat Example"


def test_onchain_labels_may_reference_a_person_entity_in_research_mode(on):
    from tests.unit.onchain.test_onchain_records import NS, WRITE, _label

    conn = duckdb.connect(":memory:")
    try:
        store, key = _label(conn)
        reference = store.propose_label_reference(NS, key, "person:jane", reason="r", principal_id="p",
                                                  scopes=WRITE)
        assert reference
        from src.kb.onchain import OnchainError

        with pytest.raises(OnchainError) as exc:  # an untyped entity is still refused
            store.propose_label_reference(NS, key, "untyped:x", reason="r", principal_id="p", scopes=WRITE)
        assert exc.value.code == "entity_type_refused"
    finally:
        conn.close()


def test_fec_individual_donors_keep_name_employer_and_occupation_but_never_the_street_or_zip(off, monkeypatch):
    counterparty, minimisation = fec_counterparty(ROW, "contributor", committee_field="contributor_id")
    assert counterparty["name"] is None and minimisation["policy"] != PERSON_RESEARCH_POLICY
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    counterparty, minimisation = fec_counterparty(ROW, "contributor", committee_field="contributor_id")
    assert counterparty["name"] == "EXAMPLE, PAT" and counterparty["employer"] == "FICTIONAL HOLDINGS"
    assert counterparty["occupation"] == "INVESTOR" and counterparty["contributor_id"] == "C00000001"
    assert "street_1" not in json_keys(counterparty) and "zip" not in json_keys(counterparty)
    assert minimisation["policy"] == PERSON_RESEARCH_POLICY
    assert minimisation["withheld"] == ["contributor_street_1", "contributor_zip"]
    record = {"minimisation": minimisation, "fields": {"counterparty": counterparty, "amount_as_reported": "1"}}
    assert minimisation_violations(record) == []
    leaky = copy.deepcopy(record)
    leaky["fields"]["contributor_zip"] = "00000"
    assert minimisation_violations(leaky) == ["$.fields.contributor_zip"]
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    assert minimisation_violations(record) == ["$.minimisation.policy"]  # never written with the switch off


def json_keys(value: dict) -> set[str]:
    return set(value)


def test_research_mode_donors_become_reviewable_identity_subjects(on):
    from src.kb.campaign_finance_identity import CampaignFinanceIdentity, donor_key
    from src.kb.campaign_finance_records import CampaignFinanceStore
    from tests.unit import campaign_finance_harness as h
    from tests.unit.domains.test_campaign_finance_records import records

    people = [r for r in records("us-fec-schedule-a") if r["minimisation"]["counterparty"] == "natural-person"]
    assert people and all(r["minimisation"]["policy"] == PERSON_RESEARCH_POLICY for r in people)
    named = [r for r in people if r["fields"]["counterparty"].get("name")]
    assert named and all(donor_key(r).startswith("campaign-finance:fec:donor-person:") for r in named)
    conn = h.connection()
    CampaignFinanceStore(conn).project(h.NS, records("us-fec-schedule-a"), run_id="r",
                                       source_id="us-fec-schedule-a")
    subjects = CampaignFinanceIdentity(conn).subjects(h.NS, scopes=h.SCOPES)
    assert any(s["kind"] == "donor-person" for s in subjects)


def test_extractives_individual_payers_keep_their_published_name_in_research_mode(on):
    from src.kb.extractives_records import check_minimised
    from tests.unit import extractives_harness as h

    individual = next(i for i in h.fetch("eiti")[0] if i["extractives_item"].get("natural_person"))
    body = copy.deepcopy(individual["extractives_item"])
    body["name_as_published"] = "A. Fixture-Person"
    check_minimised(body)  # accepted while the switch is on


def test_bafin_offers_cross_issuer_person_candidates_in_research_mode(on, monkeypatch):
    from src.domains.market.bafin_identity import BafinIdentity, person_key
    from tests.unit import bafin_harness as h
    from tests.unit.domains.test_bafin_identity_ownership import world

    conn = world()
    identity = BafinIdentity(conn)
    real = identity.parties(h.NS)

    def parties(namespace):
        out = dict(real)
        for issuer in (h.ISSUER, h.OTHER_ISSUER):
            key = person_key(issuer, "Erika Musterfrau")
            out[key] = {"record_key": key, "kind": "natural_person", "names": ["Erika Musterfrau"],
                        "roles": ["notifier"], "issuers": [issuer], "leis": [], "countries": [], "bafin_ids": [],
                        "revisions": sorted(next(iter(real.values()))["revisions"])}
        return out

    monkeypatch.setattr(identity, "parties", parties)
    result = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    pair = {person_key(h.ISSUER, "Erika Musterfrau"), person_key(h.OTHER_ISSUER, "Erika Musterfrau")}
    cross = [c for c in result["candidates"] if set(c["records"]) == pair]
    assert cross and cross[0]["state"] == "proposed" and cross[0]["basis"] == "similar-name"


def test_the_osint_dossier_assembles_registry_and_ledger_sections_for_a_person_in_research_mode(on):
    from src.kb import onchain
    from src.kb.onchain import OnchainStore
    from src.osint.dossier import entity_dossier
    from tests.unit.onchain import fixture_builder as fb

    conn = duckdb.connect(":memory:")
    for source in fb.RAW_FILES:
        fb.run_journey(conn, source, namespace="onchain")
    conn.execute("CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
                 "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)")
    conn.execute("CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT NOT NULL, "
                 "entity_type TEXT, created_at BIGINT NOT NULL)")
    conn.execute("INSERT INTO canonical_entities VALUES ('person:jane','Jane Example','person',1)")
    store = OnchainStore(conn)
    key = f"eip155:1|{fb.lower('exampla-token-contract')}|ethereum-lists-tokens|token"
    ref = store.propose_label_reference("onchain", key, "person:jane", reason="the label names her",
                                        principal_id="analyst", scopes=fb.SCOPES)
    store.review_label_reference("onchain", ref["reference_id"], "accept", "checked", principal_id="reviewer",
                                 scopes=fb.SCOPES)
    out = entity_dossier(conn, "person:jane", entity_type="person",
                         onchain={"namespace": "onchain", "scopes": {onchain.READ_SCOPE}})
    assert out["is_person"] is True and "code" not in out
    assert out["onchain"]["status"] == "assembled"
    assert out["onchain"]["contracts"][0]["deployment"]["citation"]["cited"] is True
    conn.close()
