"""Competition case, stage, party, document and state-aid-award records (#2217, CS02)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.competition import CompetitionStore
from src.kb.competition_records import (
    CONTRACT,
    CompetitionRecordError,
    award_key,
    case_key,
    child_key,
    record,
    schema,
    validate_record,
)
from tests.unit import competition_harness as h

SOURCE = {"provider": "ec-competition", "provider_record_id": "M.99001",
          "url": "https://competition-cases.ec.europa.eu/cases/M.99001"}


def case(**extra):
    return record("competition_case", case_key("ec", "M.99001"), SOURCE, authority="ec", case_number="M.99001",
                  instrument="merger", instrument_as_published="Merger", title="EXAMPLA / NORTHWIND", **extra)


def test_every_kind_round_trips_through_validation_and_the_json_schema():
    validator = Draft202012Validator(schema())
    Draft202012Validator.check_schema(schema())
    parent = case_key("ec", "M.99001")
    items = [
        case(opened_on="2025-02-03", state_as_published="Open"),
        record("case_stage", child_key("stage", parent, "Notification", "2025-02-03"), SOURCE, case_key=parent,
               authority="ec", case_number="M.99001", stage_as_published="Notification", stage_date="2025-02-03"),
        record("case_party", child_key("party", parent, "Exampla Holdings plc", "Notifying party"), SOURCE,
               case_key=parent, authority="ec", case_number="M.99001", name_as_published="Exampla Holdings plc",
               role="notifying_party", role_as_published="Notifying party", country="GB"),
        record("decision_document", child_key("document", parent, "https://ec.europa.eu/x.pdf"), SOURCE,
               case_key=parent, authority="ec", case_number="M.99001", document_type_as_published="Art. 8(2)",
               document_date="2025-09-19", language="EN", url="https://ec.europa.eu/x.pdf",
               citation={"celex": "32025M99001", "oj_reference": "OJ C 401, 1.10.2025, p. 5"}),
        record("state_aid_award", award_key("NL", "NL-2025-000101"),
               {"provider": "eu-tam", "provider_record_id": "NL:NL-2025-000101"}, award_id="NL-2025-000101",
               member_state="NL", sa_number="SA.99002", beneficiary_name_as_published="Exampla Intermediate B.V.",
               amount_as_published="12500000.00", currency="EUR", award_date="2025-05-02", status="published"),
    ]
    for item in items:
        assert validate_record(json.loads(json.dumps(item))) == item
        assert not list(validator.iter_errors(item)), item["kind"]
    assert items[1]["date_status"] == "stated" and "document" in items[1]["unknowns"]
    assert items[4]["unknowns"] == ["beneficiary_identifiers"]


def test_published_wording_is_kept_and_nothing_is_resolved_or_assessed():
    party = record("case_party", "competition:party:ec:M.99001:x", SOURCE, case_key=case_key("ec", "M.99001"),
                   authority="ec", case_number="M.99001", name_as_published="Northwind Widgets GmbH", role="target",
                   role_as_published="Target undertaking")
    assert party["unknowns"] == ["country", "identifiers"] and "canonical_entity_id" not in party
    for field in ("outcome_prediction", "market_power", "compatibility_assessment", "legal_advice"):
        with pytest.raises(CompetitionRecordError) as exc:
            validate_record({**case(), field: "x"})
        assert exc.value.code == "assessment_forbidden"
    with pytest.raises(CompetitionRecordError):
        record("state_aid_award", award_key("NL", "1"), {"provider": "eu-tam", "provider_record_id": "1"},
               award_id="1", member_state="NL", beneficiary_name_as_published="X", amount_as_published=12.5,
               status="published")  # amounts are published text, never converted numbers
    with pytest.raises(CompetitionRecordError):
        record("case_stage", "competition:stage:ec:M.99001:y", SOURCE, case_key=case_key("ec", "M.99001"),
               authority="ec", case_number="M.99001", stage_as_published="Notification", date_status="unknown",
               stage_date="2025-01-01")


def test_records_persist_through_the_ownership_store_with_revisions():
    conn = h.connection()
    store = CompetitionStore(conn)
    first = case(state_as_published="Open")
    assert store.apply(h.NS, [first], run_id="r1") == {"inserted": 1, "revised": 0, "unchanged": 0}
    assert store.apply(h.NS, [first], run_id="r2") == {"inserted": 0, "revised": 0, "unchanged": 1}
    assert store.apply(h.NS, [case(state_as_published="Closed")], run_id="r3")["revised"] == 1
    history = store.history(h.NS, case_key("ec", "M.99001"))
    assert [v["record"]["state_as_published"] for v in history] == ["Open", "Closed"]
    assert {r[0] for r in conn.execute("SELECT kind FROM ownership_records").fetchall()} == {"competition_case"}
    assert history[0]["record"]["contract"] == CONTRACT


def test_the_schema_registers_as_a_versioned_module():
    from src.kb.competition_records import register_schemas

    conn = h.connection()
    result = register_schemas(conn, principal_id="operator", scopes={"operator", "knowledge:schema:register",
                                                                      "knowledge:schema:read"})
    assert result and json.dumps(result[0]).count("competition-record")
