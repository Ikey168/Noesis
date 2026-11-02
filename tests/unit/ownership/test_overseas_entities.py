"""UK Register of Overseas Entities (#2886): beneficial owners, managing officers, statuses and the dossier.

Offline, over authored fixtures about fictional overseas entities and persons.
"""

import json

from src.ingestion.ownership_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.ownership_dossier import build_dossier, export_dossier
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES

# ---------------------------------------------------- #2886 overseas entities


def test_roe_beneficial_owners_managing_officers_and_statuses_as_published(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    records = harness.flat_records(harness.drain_source("companies-house-roe"))
    entity = records["companies-house:gb-coh:OE099901"]
    assert entity["jurisdiction"] == "JE" and entity["native"]["overseas_entity"]["foreign_company_details"][
        "registration_number"] == "999901"
    assert records["companies-house:gb-coh:OE099902"]["jurisdiction"] == "VG"
    roles = {r["officer"]["name"]: r for r in records.values() if r["kind"] == "officer_role"}
    assert roles["VOE, Lee"]["role"] == "managing-officer"
    assert roles["EXAMPLA SECRETARIES LIMITED"]["officer"]["kind"] == "entity"
    assert roles["EXAMPLA SECRETARIES LIMITED"]["resigned_on"] == "2025-06-30"
    individual = records["companies-house:psc:OE099901:FIXBO001:"
                         "ownership-of-shares-more-than-25-percent-registered-overseas-entity"]
    assert individual["owner_scoped"] is True and individual["share"]["band"]["max"] is None
    person = records["companies-house:psc:OE099901:FIXBO001"]
    assert person["kind"] == "person" and person["owner_scoped"] is True
    assert "date_of_birth" not in person["native"] and "St Helier" not in json.dumps(person)
    corporate = records["companies-house:psc:OE099901:FIXBO002:"
                        "right-to-appoint-and-remove-directors-registered-overseas-entity"]
    assert corporate["holder"]["key"] == "companies-house:gb-coh:09990001"
    assert corporate["basis"].startswith("Register of Overseas Entities")
    trust = records["companies-house:psc:OE099901:FIXBO003:"
                    "ownership-of-shares-more-than-25-percent-as-trust-registered-overseas-entity"]
    assert trust["native"]["held_as"] == "trust" and trust["validity"]["to"] == "2025-03-31"
    government = records["companies-house:psc:OE099901:FIXBO004"]
    assert government["kind"] == "legal_entity" and government["entity_type"] == "Government department"
    sanctioned = records["companies-house:psc:OE099901:FIXBO004:significant-influence-or-control-registered-overseas-entity"]
    assert sanctioned["native"]["is_sanctioned_as_declared"] is True  # as declared; no Noesis determination
    secure = records["companies-house:psc:OE099901:FIXBO005:super-secure"]
    assert secure["assertion_kind"] == "reporting_exception" and secure["holder"] is None
    assert secure["reporting_exception"]["category"] == "super-secure-beneficial-owner"
    statement = next(r for r in records.values() if r["record_key"].startswith("companies-house:psc-statement:OE099902"))
    assert statement["reporting_exception"]["category"] == "fixture-no-registrable-beneficial-owner-identified"


def test_overseas_entity_dossier_cites_its_beneficial_owners_and_lists_no_titles():
    env = harness.Env().ready()
    dossier = build_dossier(env.conn, NS, "gb-coh", "OE099901", principal_id=harness.PRINCIPAL, scopes=SCOPES,
                            as_of="2025-06-01", evidence_kind="offline-fixture")
    assert dossier["status"] == "assembled"
    parents = dossier["relationships"]["direct_parents"]
    holders = {g["holder"]: g for g in parents["groups"]}
    assert "companies-house:gb-coh:09990001" in holders and "companies-house:psc:OE099901:FIXBO004" in holders
    persons = parents["other_holdings"]["person_or_unidentified_holders"]
    assert {p["holder"]["key"] for p in persons} == {"companies-house:psc:OE099901:FIXBO001"}
    assert {e["reporting_exception"]["category"] for e in parents["reporting_exceptions"]} == {
        "super-secure-beneficial-owner"}
    assert any(e["provider"] == "companies-house" for e in parents["excluded"])  # the ceased trust interest
    assert {o["officer"]["name"] for o in dossier["officers"]} == {"VOE, Lee", "EXAMPLA SECRETARIES LIMITED"}
    assert "title" not in json.dumps(dossier["entities"]).lower()
    assert PROVIDER_CONTRACTS["hmlr-ocod"]["status"] == "not-implemented"
    assert "title numbers" in PROVIDER_CONTRACTS["hmlr-ocod"]["reason"]
    exported = export_dossier(env.conn, dossier, "oe099901", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    cited = {a["record_id"] for g in parents["groups"] for a in g["assertions"]}
    assert exported["report_id"] and exported["citations"] >= len(cited)


def test_further_register_contracts_are_recorded_honestly():
    roe = PROVIDER_CONTRACTS["companies-house-roe"]
    assert roe["status"] == "implemented" and "trust" in roe["terms"] and roe["audit"]
    ocod = PROVIDER_CONTRACTS["hmlr-ocod"]
    assert ocod["status"] == "not-implemented" and "account" in ocod["reason"] and "spatial" in ocod["reason"]
    assert LIVE_VERIFICATION["hmlr-ocod"]["status"] == "not-implemented"
