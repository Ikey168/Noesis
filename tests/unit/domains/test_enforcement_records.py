"""Enforcement authority, action, respondent, notice, penalty and appeal records with revisions (#2651, EN02)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.enforcement import EnforcementError, EnforcementStore
from src.kb.enforcement_records import (
    CONTRACT,
    EnforcementRecordError,
    action_key,
    authority_key,
    child_key,
    record,
    schema,
    validate_record,
)
from tests.unit import enforcement_harness as h

SOURCE = {"provider": "us-sec", "provider_record_id": "LR-99901",
          "url": "https://www.sec.gov/enforcement-litigation/litigation-releases/lr-99901"}
ACTION = action_key("us-sec", "LR-99901")


def action(**extra):
    fields = {"authority": "us-sec", "action_number": "LR-99901", "action_type": "civil_action",
              "action_type_as_published": "Litigation Release", "title": "Exampla Holdings plc",
              "published_on": "2025-06-10", "legal_bases": ["Section 10(b) of the Securities Exchange Act of 1934"],
              "settlement": {"settled_as_published": True,
                             "admission_as_published": "without admitting or denying the allegations"},
              "natural_person_respondents": 1}
    fields.update(extra)
    return record("enforcement_action", ACTION, SOURCE, **fields)


def child(kind, *parts, **fields):
    return record(kind, child_key(kind, ACTION, *parts), SOURCE, action_key=ACTION, authority="us-sec",
                  action_number="LR-99901", **fields)


def test_every_kind_round_trips_through_validation_and_the_json_schema():
    validator = Draft202012Validator(schema())
    Draft202012Validator.check_schema(schema())
    items = [
        record("authority", authority_key("us-sec"), SOURCE, authority="us-sec",
               name_as_published="U.S. Securities and Exchange Commission", jurisdiction="US"),
        action(),
        child("respondent", "Exampla Holdings plc", name_as_published="Exampla Holdings plc",
              respondent_type="organisation", role="defendant", role_as_published="Defendant",
              identifiers=[{"scheme": "sec-cik", "value": "0009999101", "type_as_published": "CIK"}]),
        child("enforcement_decision", "u", document_type_as_published="Litigation Release",
              document_date="2025-06-10", url=SOURCE["url"], content_sha256="a" * 64),
        child("penalty", "civil_penalty", penalty_type="civil_penalty", penalty_type_as_published="civil penalty",
              amount_as_published="$2,500,000", currency="USD", amount_status="stated"),
        child("penalty", "disgorgement", penalty_type="disgorgement", penalty_type_as_published="disgorgement",
              amount_status="not_published"),
        child("appeal", "x", forum_as_published="Upper Tribunal", reference="FS/2023/0017",
              status_as_published="dismissed", decided_on="2024-09-30"),
    ]
    for item in items:
        assert validate_record(json.loads(json.dumps(item))) == item
        assert not list(validator.iter_errors(item)), item["kind"]
    assert items[1]["unknowns"] == ["decided_on", "initiated_on", "outcome_as_published"]
    assert items[5]["unknowns"] == ["amount"] and items[5].get("amount_as_published") is None
    assert items[6]["unknowns"] == ["lodged_on"]


def test_outcomes_stay_as_published_and_no_assessment_or_finding_is_stored():
    settled = action()
    assert settled["settlement"]["admission_as_published"] == "without admitting or denying the allegations"
    for field in ("risk_score", "compliance_score", "finding_of_wrongdoing", "violation_found", "guilty",
                  "legal_advice"):
        with pytest.raises(EnforcementRecordError) as exc:
            validate_record({**action(), field: "x"})
        assert exc.value.code == "assessment_forbidden"
    with pytest.raises(EnforcementRecordError):
        action(settlement={"settled_as_published": True, "finding": "liable"})
    with pytest.raises(EnforcementRecordError):  # amounts are published text, never converted numbers
        child("penalty", "p", penalty_type="civil_penalty", penalty_type_as_published="civil penalty",
              amount_as_published=2500000, currency="USD", amount_status="stated")
    with pytest.raises(EnforcementRecordError):  # an undisclosed penalty carries no figure
        child("penalty", "p", penalty_type="civil_penalty", penalty_type_as_published="civil penalty",
              amount_as_published="$1", amount_status="not_published")


def test_minimisation_is_enforced_at_write_time():
    with pytest.raises(EnforcementRecordError) as exc:
        child("respondent", "Jordan Placeholder", name_as_published="Jordan Placeholder",
              respondent_type="natural_person", role="defendant", role_as_published="Defendant")
    assert exc.value.code == "minimisation_violation"
    for field in ("date_of_birth", "home_address", "nationality", "natural_person_names"):
        with pytest.raises(EnforcementRecordError) as exc:
            validate_record({**action(), "native": {field: "x"}})
        assert exc.value.code == "minimisation_violation"
    with pytest.raises(EnforcementRecordError):
        action(natural_person_respondents=["Jordan Placeholder"])
    store = EnforcementStore(h.connection())
    with pytest.raises(EnforcementError) as exc:
        store.apply(h.NS, [{**action(), "native": {"date_of_birth": "1970-01-01"}}], run_id="r")
    assert exc.value.code == "minimisation_violation"


def test_revision_chain_as_of_lookup_and_removal_is_a_revision():
    conn = h.connection()
    clock = h.Clock()
    store = EnforcementStore(conn, now=clock)
    assert store.apply(h.NS, [action()], run_id="r1", observed_at_ms=1000) == {"inserted": 1, "revised": 0,
                                                                                "unchanged": 0}
    assert store.apply(h.NS, [action()], run_id="r2", observed_at_ms=2000)["unchanged"] == 1
    corrected = action(decided_on="2025-07-01", outcome_as_published="Final judgment entered",
                       source_status="corrected")
    assert store.apply(h.NS, [corrected], run_id="r3", observed_at_ms=3000)["revised"] == 1
    removal = {"contract": CONTRACT, "kind": "removal", "record_key": ACTION, "provider": "us-sec",
               "action_number": "LR-99901", "http_status": 410, "evidence_origin": "fixture"}
    assert store.apply(h.NS, [removal], run_id="r4", observed_at_ms=4000)["revised"] == 1
    chain = store.history(h.NS, ACTION)
    assert [v["record"]["source_status"] for v in chain] == ["published", "corrected", "removed_by_source"]
    assert [v["revision"] for v in chain] == [1, 2, 3]
    assert chain[2]["record"]["decided_on"] == "2025-07-01"  # the removal carries the last content forward
    # As-of lookup by record time: what the store held then.
    assert store.by_key(h.NS, ACTION, known_at_ms=2500)["record"]["source_status"] == "published"
    assert store.by_key(h.NS, ACTION, known_at_ms=3500)["record"]["decided_on"] == "2025-07-01"
    assert store.by_key(h.NS, ACTION, known_at_ms=500) is None
    assert [v["record"]["source_status"] for v in store.views(h.NS, ("enforcement_action",), known_at_ms=2500)] == [
        "published"]
    # Nothing is ever deleted: every revision row is still there.
    assert conn.execute("SELECT count(*) FROM enforcement_record_revisions").fetchone()[0] == 3
    orphan = {**removal, "record_key": action_key("us-sec", "LR-00000"), "action_number": "LR-00000"}
    assert store.apply(h.NS, [orphan], run_id="r5")["removal_unmatched"] == 1


def test_the_schema_registers_as_a_versioned_module():
    from src.kb.enforcement_records import register_schemas

    conn = h.connection()
    result = register_schemas(conn, principal_id="operator", scopes={"operator", "knowledge:schema:register",
                                                                      "knowledge:schema:read"})
    assert result and json.dumps(result[0]).count("enforcement-record")
