"""E10: operators to canonical entities / LEI through reviewable decisions; evidence beside policy obligations."""

import pytest

from src.kb.assertions import record_assertions
from src.kb.entities import add_manual_alias
from src.kb.environment_identity import EnvironmentIdentity
from src.kb.environment_store import EnvironmentStoreError, record_id
from src.kb.lei import LeiStore
from src.policy_monitor.environment_evidence import obligation_evidence
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, REVIEWER_SCOPES, SCOPES

SUBJECT = "policy:e-prtr-reporting-thresholds"


def lei_with_check_digits(prefix):
    number = int("".join(str(int(ch, 36)) for ch in prefix + "00"))
    return prefix + f"{98 - number % 97:02d}"


FIXTURE_LEI = lei_with_check_digits("5299FIXTUREWAERME0")


@pytest.fixture
def env():
    env = harness.Env()
    for provider in ("eea-industry", "eu-ets"):
        assert env.acquire(provider)["ok"]
    LeiStore(env.conn).observe_page(NS, [{"lei_record": {
        "lei": FIXTURE_LEI, "part": "record", "raw_sha256": "fixture",
        "attributes": {"lei": FIXTURE_LEI, "entity": {"legalName": {"name": "Beispiel Wärme Berlin GmbH"},
                                                      "registeredAs": "HRB 000001 B", "jurisdiction": "DE"},
                       "registration": {"status": "ISSUED", "lastUpdateDate": "2026-01-01T00:00:00Z"}}}}],
        run_id="lei-fixture", page_receipt={"part": "record"})
    add_manual_alias(env.conn, "Beispiel Kraftwerke Nord", "Beispiel Kraftwerke Nord AG", "organization")
    record_assertions(env.conn, SUBJECT, {"co2_air_threshold": {
        "value": "100000", "unit": "t", "per": "year", "pollutant": "CO2", "medium": "AIR",
        "source": "authored fixture citing Regulation (EC) No 166/2006 Annex II"}},
        effective_at_ms=1, document_id="policy:e-prtr-annex-ii-fixture", visibility="public", record_kind="primary_text")
    return env


def test_operators_get_candidate_decisions_and_unmatched_stay_source_strings(env):
    identity = EnvironmentIdentity(env.conn)
    proposed = identity.propose_operator_links(NS, principal_id="alice", scopes=SCOPES)
    by_target = {(c["target_kind"], c["operator_name"]): c for c in proposed["candidates"]}
    ets = by_target[("lei", "Beispiel Wärme Berlin GmbH")]
    assert ets["target_id"] == FIXTURE_LEI and ets["state"] == "candidate"
    assert {c["basis"] for c in proposed["candidates"] if c["target_kind"] == "lei"} == {
        "registration-number match (operator record vs LEI registeredAs)", "normalized legal-name match"}
    entity = by_target[("canonical-entity", "Beispiel Kraftwerke Nord AG")]
    assert entity["target_id"].startswith("ent-") and entity["state"] == "candidate"
    unmatched = {u["operator"] for u in proposed["unmatched"]}
    assert "Muster Stadtwerke Fixture GmbH" in unmatched
    assert not any(c["state"] == "accepted" for c in proposed["candidates"])  # nothing is auto-accepted
    again = identity.propose_operator_links(NS, principal_id="alice", scopes=SCOPES)
    assert again["created"] == []


def test_review_is_by_another_principal_and_accepted_links_show_on_the_facility(env):
    identity = EnvironmentIdentity(env.conn)
    link = next(c for c in identity.propose_operator_links(NS, principal_id="alice", scopes=SCOPES)["candidates"]
                if c["basis"].startswith("registration-number"))
    with pytest.raises(EnvironmentStoreError) as own:
        identity.review(NS, link["link_id"], "accepted", "same register number", principal_id="alice", scopes=REVIEWER_SCOPES)
    assert own.value.code == "self_review"
    with pytest.raises(EnvironmentStoreError) as scope:
        identity.review(NS, link["link_id"], "accepted", "x", principal_id="bob", scopes=SCOPES)
    assert scope.value.code == "unauthorized"
    reviewed = identity.review(NS, link["link_id"], "accepted", "HRB number matches the LEI registeredAs",
                               principal_id="bob", scopes=REVIEWER_SCOPES)
    assert reviewed["state"] == "accepted" and reviewed["reviewed_by"] == "bob"
    from src.kb.environment_identity import operator_view

    view = operator_view(env.conn, NS, link["facility_record"], {"name": "Beispiel Wärme Berlin GmbH"})
    assert view["state"] == "linked" and view["links"][0]["target_id"] == FIXTURE_LEI
    assert view["source_name"] == "Beispiel Wärme Berlin GmbH"  # the published string is kept


def test_facility_release_is_evidence_beside_an_obligation_without_a_determination(env):
    identity = EnvironmentIdentity(env.conn)
    facility = record_id(NS, "facility", "eea-industry", "DE.UBA.PRTR/000000901.FACILITY")
    series = record_id(NS, "observation_series", "eea-industry", "DE.UBA.PRTR/000000901.FACILITY:CO2:AIR")
    evidence = identity.attach_evidence(NS, subject_id=SUBJECT, assertion_key="co2_air_threshold",
                                        facility_record=facility, series_record=series, period_start="2024-01-01",
                                        principal_id="alice", scopes=SCOPES)
    assert evidence["determination"] is None and "No compliance determination" in evidence["notice"]
    assert evidence["obligation"]["document_id"] == "policy:e-prtr-annex-ii-fixture"
    release = evidence["evidence"]["release"]
    assert release["value"] == "612000000" and release["unit"] == "kg"
    assert release["converted_to_obligation_unit"]["value"] == "612000.000000"  # pint kg -> t, shown not judged
    assert evidence["evidence"]["vintage"]["vintage_id"].startswith("env-vintage:")
    view = obligation_evidence(env.conn, SUBJECT, namespace=NS, scopes=SCOPES)
    assert view["determination"] is None and len(view["evidence"]) == 1 and view["obligations"][0]["assertion_key"] == "co2_air_threshold"
    with pytest.raises(EnvironmentStoreError):
        identity.attach_evidence(NS, subject_id=SUBJECT, assertion_key="co2_air_threshold", facility_record=facility,
                                 series_record=record_id(NS, "observation_series", "eu-ets", "DE_900201:verified_emissions"),
                                 period_start="2025-01-01", principal_id="alice", scopes=SCOPES)
    with pytest.raises(EnvironmentStoreError):
        identity.attach_evidence(NS, subject_id=SUBJECT, assertion_key="missing", facility_record=facility,
                                 series_record=series, period_start="2024-01-01", principal_id="alice", scopes=SCOPES)
