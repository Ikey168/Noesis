"""Person-research mode (``NOESIS_PERSON_RESEARCH``) for enforcement (EN01) and courts (CJ01), #2892.

With the switch off every enforcement and courts test keeps passing unchanged; these tests pin what the mode
changes: individual respondents and parties keep their published names, cited and matchable to ownership persons
through reviewed identity, while dates of birth, home addresses and personal registration numbers stay withheld and
records stored under the mode are refused on write with the switch off. Every name, number and address is fictional.
"""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from src.ingestion.source_packs import SourcePackError
from tests.unit import courts_justice_harness as ch
from tests.unit import enforcement_harness as h

ENF_POLICY = "enforcement-person-research-v1"
COURTS_POLICY = "courts-person-research-v1"
JORDAN = "companies-house:person:fixture-jordan-placeholder"
JANE = "open-ownership:person:fixture-jane-roe"
FCA_PATH = "/publication/final-notices/northwind-brokers-limited-2024.pdf"
ECHO_PATH = "/echo/case_rest_services.get_case_info?output=JSON&p_id=09-2025-9901"
# An FCA final notice addressed to an individual, with the personal details a real one carries in its header.
FCA_INDIVIDUAL = """FINAL NOTICE

To: Jo Placeholder
Individual Reference Number: JXP01234
Date of birth: 1 January 1970
Address: 1 Fictional Way, Example Town
Date: 18 November 2024

1. ACTION
1.1. For the reasons given in this Final Notice, the Authority hereby imposes on Jo Placeholder a financial penalty of £35,000 pursuant to section 66 of the Act.

2. SUMMARY OF REASONS
2.1. Jo Placeholder (IRN JXP01234), born on 1 January 1970 and residing at 1 Fictional Way, Example Town, failed to act with integrity in breach of Statement of Principle 1.

3. DEFINITIONS
3.1. "the Act" means the Financial Services and Markets Act 2000.
"""


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)


def _by_kind(records):
    out: dict[str, list] = {}
    for record in records:
        out.setdefault(record["kind"], []).append(record)
    return out


def _echo_individual_only() -> str:
    body = json.loads((h.FIXTURES / "echo_09-2025-9901.json").read_text())
    body["Results"].update({"CaseName": "Casey Placeholder", "Defendants": [{"DefendantName": "Casey Placeholder"}]})
    return json.dumps(body)


def _person(conn, namespace, key, name, country):
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    provider = key.split(":", 1)[0]
    OwnershipStore(conn).apply(namespace, [record("person", key, {"provider": provider, "provider_record_id": key},
                                                  name=name, country_of_residence=country, identifiers=[])],
                               run_id="seed-person", observed_at_ms=1, principal_id="operator")


# ------------------------------------------------------------------ enforcement


def test_enforcement_individuals_are_named_and_cited_only_in_research_mode(off, monkeypatch):
    default = json.dumps(h.records("sec-enforcement-releases"))
    assert "Jordan Placeholder" not in default and "[individual]" in default
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    items = _by_kind(h.records("sec-enforcement-releases"))
    action = next(a for a in items["enforcement_action"] if a["record_key"] == h.SEC_LR)
    assert action["title"] == "Exampla Holdings plc and Jordan Placeholder"
    assert action["natural_person_respondents"] == 1 and action["minimisation"]["policy"] == ENF_POLICY
    assert action["court_cases"][0]["caption"] == "Exampla Holdings plc and Jordan Placeholder"
    assert any("Jordan Placeholder" in c for c in action["charges_as_published"])
    assert "[individual]" not in json.dumps(items)
    person = next(r for r in items["respondent"] if r["name_as_published"] == "Jordan Placeholder")
    assert person["respondent_type"] == "natural_person" and person["identifiers"] == []
    assert person["role_as_published"] == "Defendant" and person["minimisation"]["policy"] == ENF_POLICY
    assert person["source"]["url"].startswith("https://www.sec.gov/") and person["action_key"] == h.SEC_LR
    # The individual's own sanction is recorded and attributed to them; the firm's stays as before.
    own = [p for p in items["penalty"] if p.get("respondent_key") == person["record_key"]]
    assert [(p["amount_as_published"], p["currency"]) for p in own] == [("$75,000", "USD")]
    firm = next(r for r in items["respondent"] if r["name_as_published"] == "Exampla Holdings plc")
    assert "minimisation" not in firm  # records naming no individual are stored exactly as with the switch off
    echo = _by_kind(h.records("epa-echo-enforcement-cases"))
    assert "Casey Placeholder" in {r["name_as_published"] for r in echo["respondent"]}


def test_individual_only_notices_and_cases_are_acquired_with_personal_details_withheld(off, monkeypatch):
    with pytest.raises(SourcePackError) as exc:
        h.fetch("fca-final-notices", bodies={FCA_PATH: FCA_INDIVIDUAL})
    assert "addressed to individuals" in str(exc.value)
    with pytest.raises(SourcePackError):
        h.fetch("epa-echo-enforcement-cases", bodies={ECHO_PATH: _echo_individual_only()})
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    records = h.records("fca-final-notices", bodies={FCA_PATH: FCA_INDIVIDUAL})
    notice = [r for r in records if r.get("action_number") == "northwind-brokers-limited-2024"
              or r.get("record_key") == h.FCA_NW]
    text = json.dumps(notice)
    assert "Jo Placeholder" in text
    for withheld in ("JXP01234", "1970", "Fictional Way"):
        assert withheld not in text, withheld
    items = _by_kind(notice)
    (action,) = items["enforcement_action"]
    assert action["title"] == "Final Notice: Jo Placeholder" and action["natural_person_respondents"] == 1
    assert action["related_identifiers"] == [] and action["minimisation"]["policy"] == ENF_POLICY
    assert any("[IRN withheld]" in c and "[date of birth withheld]" in c and "[home address withheld]" in c
               for c in action["charges_as_published"])
    (respondent,) = items["respondent"]
    assert respondent["respondent_type"] == "natural_person" and respondent["identifiers"] == []
    assert all(r["minimisation"]["policy"] == ENF_POLICY for r in notice)
    echo = h.records("epa-echo-enforcement-cases", bodies={ECHO_PATH: _echo_individual_only()})
    case = next(r for r in echo if r["record_key"] == h.EPA_EX)
    assert case["title"] == "Casey Placeholder" and case["natural_person_respondents"] == 1


def test_person_research_records_are_refused_on_write_with_the_switch_off(off, monkeypatch):
    from src.kb.enforcement import EnforcementError, EnforcementStore
    from src.kb.enforcement_records import (
        EnforcementRecordError,
        schema,
        validate_record,
    )

    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    records = h.records("sec-enforcement-releases")
    person = next(r for r in records if r.get("respondent_type") == "natural_person")
    validator = Draft202012Validator(schema())
    for record in records:
        validator.validate(record)
    anonymous = {k: v for k, v in person.items() if k != "minimisation"}
    assert list(validator.iter_errors(anonymous))  # a named individual needs the person-research policy
    with pytest.raises(EnforcementRecordError) as exc:
        validate_record(anonymous)
    assert exc.value.code == "minimisation_violation"
    for leak in ({"identifiers": [{"scheme": "gb-fca-irn", "value": "JXP01234"}]},
                 {"identifiers": [{"scheme": "sec-cik", "value": "0009999999"}]}, {"native": {"date_of_birth": "x"}}):
        with pytest.raises(EnforcementRecordError) as exc:
            validate_record({**copy.deepcopy(person), **leak})
        assert exc.value.code == "minimisation_violation", leak
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    conn = h.connection()
    try:
        with pytest.raises(EnforcementError) as exc:
            EnforcementStore(conn).apply(h.NS, records, run_id="off")
        assert exc.value.code == "minimisation_violation"
        assert EnforcementStore(conn).views(h.NS) == []  # nothing of the page was written
    finally:
        conn.close()


def test_individual_respondents_match_ownership_persons_through_reviewed_identity(on, monkeypatch):
    from src.kb.enforcement_identity import EnforcementIdentity
    from src.kb.enforcement_queries import actions_for_entity

    conn = h.connection()
    try:
        h.ownership(conn)
        _person(conn, h.OWN_NS, JORDAN, "Jordan Placeholder", "GB")
        h.load_all(conn)
        identity = EnforcementIdentity(conn)
        proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
        mine = [c for c in proposed["candidates"] if c["ownership_key"] == JORDAN]
        assert len(mine) == 1 and mine[0]["state"] == "proposed" and mine[0]["low_evidence"] is True
        subject = next(s for s in identity.subjects(h.NS, scopes=h.SCOPES) if s["key"] == mine[0]["subject_key"])
        assert subject["name_as_published"] == "Jordan Placeholder" and subject["respondent_type"] == "natural_person"
        # Organisations are never offered to persons, individuals never to legal entities.
        assert all(c["subject_key"] == subject["key"] for c in proposed["candidates"]
                   if c["ownership_key"] == JORDAN)
        assert all(c["ownership_key"] == JORDAN for c in proposed["candidates"] if c["subject_key"] == subject["key"])
        before = actions_for_entity(conn, h.NS, JORDAN, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
        assert before["status"] == "no_action_on_record"  # nothing reaches the person before a reviewer accepts
        identity.review(h.NS, mine[0]["candidate_id"], "accept", "same person in the register",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
        answer = actions_for_entity(conn, h.NS, JORDAN, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
        (row,) = answer["actions"]
        assert row["action_key"] == h.SEC_LR and row["respondent"]["name_as_published"] == "Jordan Placeholder"
        assert row["respondent"]["cite"]["url"].startswith("https://www.sec.gov/")
        assert row["matched_through"]["reviewer"] == "reviewer"
        assert row["cites"]["identity_decision"] and row["cites"]["respondent_revision_id"]
        monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
        assert subject["key"] not in {s["key"] for s in identity.subjects(h.NS, scopes=h.SCOPES)}
    finally:
        conn.close()


# ------------------------------------------------------------------ courts


def _docket():
    return next(item["court_justice_record"] for page in ch.fetch("courtlistener-dockets") for item in page.records
                if item["court_justice_record"]["record_kind"] == "docket")


def test_court_parties_keep_their_published_name_and_key_only_in_research_mode(off, monkeypatch):
    default = _docket()
    assert default["fields"]["parties"][1]["name_as_published"] is None and "minimisation" not in default
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    record = _docket()
    party = record["fields"]["parties"][1]
    assert party == {"ordinal": 2, "party_type": "natural_person", "name_as_published": "Jane Roe",
                     "roles": ["Defendant"], "party_key": "courts:party:courtlistener:70001:2"}
    assert record["minimisation"]["policy"] == COURTS_POLICY
    text = json.dumps(record)
    for withheld in ("Private Lane", "2060", "7300002", "Counsel Fixture"):
        assert withheld not in text, withheld
    validator = Draft202012Validator(json.loads(
        (ch.ROOT / "contracts/schemas/jsonschema/noesis-court-justice-record-v1.json").read_text()))
    validator.validate(record)
    validator.validate(default)
    unpoliced = {k: v for k, v in record.items() if k != "minimisation"}
    assert list(validator.iter_errors(unpoliced))
    # Projection refuses the person-research docket with the switch off.
    from src.kb.courts_justice import CourtsJusticeError, CourtsJusticeProjector

    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    conn = ch.connection()
    try:
        with pytest.raises(CourtsJusticeError) as exc:
            CourtsJusticeProjector(conn).project(ch.NS, [record], run_id="off", source_id="courtlistener-dockets")
        assert exc.value.code == "minimisation_violation"
        with pytest.raises(CourtsJusticeError) as exc:  # a named person without the policy stays refused
            CourtsJusticeProjector(conn).project(ch.NS, [unpoliced], run_id="off", source_id="courtlistener-dockets")
        assert exc.value.code == "minimisation_violation"
    finally:
        conn.close()


def test_court_parties_are_matchable_queryable_and_monitorable_in_research_mode(on, monkeypatch):
    from src.kb.courts_justice import CourtsJusticeError
    from src.kb.courts_justice_identity import CourtsIdentity
    from src.kb.courts_justice_monitoring import CourtsJusticeMonitor
    from src.kb.legal_dockets import LegalDocketStore

    conn = ch.connection()
    try:
        ch.load_all(conn)
        ch.seed_ownership(conn)
        _person(conn, ch.NS, JANE, "Jane Roe", "US")
        row = conn.execute("SELECT party_type, name_as_published, pseudonym, party_key FROM legal_docket_parties "
                           "WHERE ordinal=2").fetchone()
        assert row == ("natural_person", "Jane Roe", None, "courts:party:courtlistener:70001:2")
        identity = CourtsIdentity(conn)
        parties = {p["party_key"]: p for p in identity.parties(ch.NS, scopes=ch.SCOPES)}
        assert parties["courts:party:courtlistener:70001:2"]["party_type"] == "natural_person"
        proposed = identity.propose(ch.NS, principal_id="analyst", scopes=ch.SCOPES, ownership_namespace=ch.NS,
                                    canonical_names=False)
        (candidate,) = [c for c in proposed["candidates"] if JANE in c["records"]]
        assert candidate["state"] == "proposed" and candidate["method"] == "name-jurisdiction"
        assert "courts:party:courtlistener:70001:2" in candidate["records"]
        assert not [c for c in proposed["candidates"] if JANE in c["records"] and
                    "courts:party:courtlistener:70001:1" in c["records"]]
        store = LegalDocketStore(conn)
        assert store.dockets_for_party(ch.NS, JANE, scopes=ch.SCOPES)["status"] == "no_docket_on_record"
        identity.review(ch.NS, candidate["candidate_id"], "accept", "same person", principal_id="reviewer",
                        scopes=ch.REVIEW_SCOPES)
        answer = store.dockets_for_party(ch.NS, JANE, scopes=ch.SCOPES)
        assert answer["status"] == "answered" and answer["dockets"][0]["record_key"] == ch.DOCKET
        assert answer["connected_by"]["matches"][0]["reviewer"] == "reviewer"
        assert answer["dockets"][0]["citation"]["locator"].startswith("https://www.courtlistener.com/")
        assert {"party_type": "natural_person", "name_as_published": "Jane Roe", "pseudonym": None,
                "roles": ["Defendant"], "party_key": "courts:party:courtlistener:70001:2"} \
            in answer["dockets"][0]["parties"]
        by_name = store.dockets_for_party(ch.NS, "Jane Roe", scopes=ch.SCOPES)
        assert by_name["status"] == "answered"
        assert by_name["connected_by"]["matches"][0]["method"] == "published-name"
        with pytest.raises(CourtsJusticeError):  # a pseudonym is never a query key
            store.dockets_for_party(ch.NS, "natural person 1 (Defendant)", scopes=ch.SCOPES)
        monitor = CourtsJusticeMonitor(conn)
        created = monitor.create(ch.NS, "jane", watch="party", key="Jane Roe", principal_id="analyst",
                                 scopes=ch.SCOPES)
        assert created["subscription_id"]
        monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
        with pytest.raises(CourtsJusticeError) as exc:
            store.dockets_for_party(ch.NS, "Jane Roe", scopes=ch.SCOPES)
        assert exc.value.code == "natural_person_not_a_query_key"
        with pytest.raises(CourtsJusticeError) as exc:
            monitor.create(ch.NS, "jane-off", watch="party", key="Jane Roe", principal_id="analyst",
                           scopes=ch.SCOPES)
        assert exc.value.code == "natural_person_not_a_target"
        assert "courts:party:courtlistener:70001:2" not in {p["party_key"] for p in
                                                             identity.parties(ch.NS, scopes=ch.SCOPES)}
    finally:
        conn.close()
