"""Enforcement action, respondent, decision, penalty, appeal and notice records with revisions (#2651, EN02)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.kb.enforcement import EnforcementError, EnforcementStore, removal_marker
from src.kb.enforcement_records import (
    CONTRACT,
    EnforcementRecordError,
    action_key,
    child_key,
    record,
    schema,
    validate_record,
)
from tests.unit import enforcement_harness as h

SOURCE = {"provider": "us-sec", "provider_record_id": "LR-99999", "url": "https://www.sec.gov/x"}
KEY = action_key("us-sec", "LR-99999")


def action(**fields):
    base = {"authority": "us-sec", "native_id": "LR-99999", "action_type": "civil_action",
            "action_type_as_published": "Litigation Release", "title": "SEC v. Fixture Co.",
            "legal_bases": ["Section 10(b) of the Securities Exchange Act of 1934"], "published_on": "2099-01-02",
            "outcome_as_published": "Without admitting or denying the allegations, Fixture Co. consented.",
            "settled": True, "admission_wording": "Without admitting or denying the allegations"}
    return record("enforcement_action", KEY, SOURCE, **{**base, **fields})


def test_records_validate_against_the_contract_and_keep_outcomes_as_published():
    body = action()
    assert body["contract"] == CONTRACT and body["publication_status"] == "published"
    jsonschema.validate(body, schema())
    assert body["admission_wording"] == "Without admitting or denying the allegations"
    assert body["unknowns"] == []
    undated = action(published_on=None, settled=None, legal_bases=[])
    assert {"dates", "settled", "legal_bases"} <= set(undated["unknowns"])
    penalty = record("penalty", child_key("penalty", KEY, "civil"), SOURCE, action_key=KEY, authority="us-sec",
                     penalty_type="civil_penalty", penalty_type_as_published="civil penalty", status="not_published")
    assert penalty["unknowns"] == ["amount"]
    jsonschema.validate(penalty, schema())


@pytest.mark.parametrize("field", ["risk_score", "compliance_score", "finding_of_wrongdoing", "found_liable",
                                   "person_profile", "legal_advice"])
def test_scores_findings_and_profiles_are_refused(field):
    with pytest.raises(EnforcementRecordError) as caught:
        validate_record({**action(), field: 1})
    assert caught.value.code == "assessment_forbidden"


def test_minimisation_is_enforced_at_write_time():
    person = {"contract": CONTRACT, "kind": "respondent", "record_key": child_key("respondent", KEY, 2),
              "source": SOURCE, "action_key": KEY, "authority": "us-sec", "ordinal": 2, "party_type": "natural_person",
              "role_as_published": "defendant"}
    with pytest.raises(EnforcementRecordError) as caught:
        validate_record({**person, "name_as_published": "Jordan Example", "pseudonym": "natural person 2"})
    assert caught.value.code == "minimisation_violation"
    with pytest.raises(EnforcementRecordError) as caught:
        validate_record(person)  # no pseudonym
    assert caught.value.code == "minimisation_violation"
    for field in ("date_of_birth", "address", "individual_reference_number"):
        with pytest.raises(EnforcementRecordError) as caught:
            validate_record({**person, "pseudonym": "natural person 2", "native": {field: "x"}})
        assert caught.value.code == "minimisation_violation"
    kept = validate_record({**person, "pseudonym": "natural person 2"})
    jsonschema.validate(kept, schema())
    with pytest.raises(EnforcementRecordError):
        validate_record({**person, "party_type": "organisation", "name_as_published": "Fixture Co.",
                         "pseudonym": "natural person 2"})


def test_penalties_are_published_figures_never_converted():
    base = {"contract": CONTRACT, "kind": "penalty", "record_key": child_key("penalty", KEY, "x"), "source": SOURCE,
            "action_key": KEY, "authority": "us-sec", "penalty_type": "civil_penalty",
            "penalty_type_as_published": "civil penalty", "status": "stated"}
    with pytest.raises(EnforcementRecordError):
        validate_record({**base, "amount": 1000})  # a number, not the published decimal text
    with pytest.raises(EnforcementRecordError):
        validate_record({**base, "status": "not_published", "amount": "1000"})
    with pytest.raises(EnforcementRecordError):
        validate_record({**base, "amount": "1000", "currency": "dollars"})
    assert validate_record({**base, "amount": "1000", "amount_as_published": "$1,000", "currency": "USD"})


def test_revision_chains_as_of_lookup_and_removal_revisions():
    conn = h.connection()
    clock = h.Clock()
    store = EnforcementStore(conn, now=clock)
    first = store.apply(h.NS, [action()], run_id="r1", observed_at_ms=100)
    assert first["inserted"] == 1
    assert store.apply(h.NS, [action()], run_id="r2", observed_at_ms=200)["unchanged"] == 1
    corrected = action(outcome_as_published="Fixture Co. consented to a final judgment.",
                       publication_status="corrected")
    assert store.apply(h.NS, [corrected], run_id="r3", observed_at_ms=300)["revised"] == 1
    history = store.history(h.NS, KEY)
    assert [v["revision"] for v in history] == [1, 2]
    assert store.by_key(h.NS, KEY, known_at_ms=250)["revision"] == 1
    assert store.by_key(h.NS, KEY)["record"]["publication_status"] == "corrected"
    removed = store.apply(h.NS, [removal_marker("us-sec", KEY, http_status=404, url="https://www.sec.gov/x",
                                                unit={"release": "LR-99999"})], run_id="r4", observed_at_ms=400)
    assert removed["removed_by_source"] == 1
    history = store.history(h.NS, KEY)
    assert [v["record"]["publication_status"] for v in history] == ["published", "corrected", "removed_by_source"]
    assert history[-1]["record"]["native"]["removal"]["http_status"] == 404
    again = store.apply(h.NS, [removal_marker("us-sec", KEY, http_status=410, url="https://www.sec.gov/x",
                                              unit={})], run_id="r5", observed_at_ms=500)
    assert again["unchanged"] == 1 and len(store.history(h.NS, KEY)) == 3
    unknown = store.apply(h.NS, [removal_marker("us-sec", action_key("us-sec", "LR-1"), http_status=404,
                                                url="https://www.sec.gov/y", unit={})], run_id="r6")
    assert unknown["removal_not_on_record"] == 1
    with pytest.raises(EnforcementError):
        store.apply(h.NS, [{"contract": "other"}], run_id="r7")


def test_scopes_are_required_for_reads_and_writes():
    conn = h.connection()
    store = EnforcementStore(conn)
    with pytest.raises(EnforcementError) as caught:
        store.put(h.NS, [action()], run_id="x", scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"
    store.put(h.NS, [action()], run_id="x", scopes=h.SCOPES)
    assert store.get(h.NS, KEY, scopes=h.READ_ONLY)["history"][0]["revision"] == 1
    with pytest.raises(EnforcementError):
        store.get(h.NS, KEY, scopes={"knowledge:legal:read"})


def test_schema_module_registers_in_the_shared_registry():
    from src.kb.enforcement_records import register_schemas

    conn = h.connection()
    registered = register_schemas(conn, principal_id="operator", scopes={"operator", "knowledge:schema:register",
                                                                         "knowledge:schema:read"})
    assert json.dumps(registered)
