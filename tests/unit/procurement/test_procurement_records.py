"""Procurement record model (P02): stages, values, deadlines, parties and schema registration."""

import copy
import json

import duckdb
import jsonschema
import pytest

from src.ingestion.procurement_providers import parse_ocds_package, parse_sam_search, parse_ted_search
from src.kb.procurement_records import (
    CONTRACT,
    ProcurementRecordError,
    cpv_code,
    cpv_relation,
    decimal_text,
    lei_of,
    party,
    record,
    register_schemas,
    validate_record,
    value,
)
from src.kb.schema_registry import READ_SCOPE, REGISTER_SCOPE, VALIDATE_SCOPE, SchemaRegistry
from tests.unit.procurement.harness import ROOT

BUYER = party("buyer", "Fixture Authority", identifiers=[{"scheme": "national", "id": "X-1"}], country="DE")


def base(**fields):
    return {"contract": CONTRACT, "record_kind": "notice", "provider": "ted", "notice_id": "n-1", "procedure_id": "p-1",
            "stage": "contract-notice", "title": "Fixture", "language": "en", "source_url": "https://ted.europa.eu/en/notice/-/detail/n-1",
            "buyer": BUYER, **fields}


def all_fixture_records():
    records = []
    for name in ("procurement-ted.json",):
        for page in json.loads((ROOT / "tests/fixtures/source_packs" / name).read_text())["native_pages"]:
            records += parse_ted_search(page["body"])["records"]
    for page in json.loads((ROOT / "tests/fixtures/procurement/ted-round2.json").read_text())["native_pages"]:
        records += parse_ted_search(page["body"])["records"]
    for name, provider in (("procurement-uk-fts.json", "uk-fts"), ("procurement-uk-cf.json", "uk-cf")):
        for page in json.loads((ROOT / "tests/fixtures/source_packs" / name).read_text())["native_pages"]:
            records += parse_ocds_package(page["body"], provider)["records"]
    records += parse_sam_search(json.loads((ROOT / "tests/fixtures/source_packs/procurement-sam.json").read_text())["native_pages"][0]["body"])["records"]
    return records


def test_estimated_and_awarded_values_stay_distinct_with_currency_and_vat_basis():
    estimated = value("100000", "EUR", "estimated", vat="excluded")
    item = validate_record(base(estimated_value=estimated, deadlines=[{"kind": "submission", "text": "x", "date": "2026-11-01"}]))
    assert item["estimated_value"]["kind"] == "estimated" and "deadlines.submission.instant" in item["unknowns"]
    with pytest.raises(ProcurementRecordError):
        validate_record(base(estimated_value=value("100000", "EUR", "awarded")))  # an award value is not an estimate
    with pytest.raises(ProcurementRecordError):
        validate_record(base(estimated_value={"amount": 100000.0, "currency": "EUR", "kind": "estimated", "vat": "unknown"}))
    with pytest.raises(ProcurementRecordError):
        validate_record(base(estimated_value={"amount": "5", "currency": None, "kind": "estimated", "vat": "unknown"}))
    assert "estimated_value" in validate_record(base())["unknowns"]
    assert decimal_text(80000) == "80000" and decimal_text("1250000.00") == "1250000" and decimal_text("0.5") == "0.5"


def test_stages_enforce_award_history_is_not_an_opportunity():
    award = {"award_id": "a", "lot_ids": [], "suppliers": [party("supplier", "S")], "awarded_value": value("1", "EUR", "awarded")}
    ok = validate_record(base(stage="award", awards=[award]))
    assert ok["stage"] == "award" and "awards.awarded_value.vat" in ok["unknowns"]
    for bad in (base(stage="award", awards=[award], deadlines=[{"kind": "submission", "text": "t", "date": "2026-01-01"}]),
                base(stage="award", awards=[award], status={"asserted": "active"}),
                base(awards=[award]),
                base(stage="award")):
        with pytest.raises(ProcurementRecordError):
            validate_record(bad)
    with pytest.raises(ProcurementRecordError):
        validate_record(base(stage="corrigendum"))  # must name the notice it changes
    assert validate_record(base(stage="corrigendum", changes={"changes_notice_id": "n-0"}))["stage"] == "corrigendum"


def test_deadlines_keep_text_and_offset_and_never_convert_a_bare_date():
    with pytest.raises(ProcurementRecordError):
        validate_record(base(deadlines=[{"kind": "submission", "text": "t", "instant": "2026-11-01T12:00:00"}]))
    with pytest.raises(ProcurementRecordError):
        validate_record(base(deadlines=[{"kind": "submission", "text": "t", "instant": "2026-11-01T12:00:00+01:00"}]))
    item = validate_record(base(deadlines=[{"kind": "submission", "text": "1 Nov 2026 12:00 CET", "instant": "2026-11-01T12:00:00+01:00",
                                            "timezone": "+01:00 (offset stated by the source)"}]))
    assert item["deadlines"][0]["text"] == "1 Nov 2026 12:00 CET"


def test_private_profile_state_cannot_enter_a_source_record():
    with pytest.raises(ProcurementRecordError) as caught:
        validate_record(base(owner_profile={"turnover": "1"}))
    assert caught.value.code == "private_leak"


def test_requirements_need_locators_and_valid_rules_and_parties_keep_source_identifiers():
    with pytest.raises(ProcurementRecordError) as caught:
        validate_record(base(requirements=[{"requirement_id": "r", "category": "exclusion", "text": "t"}]))
    assert caught.value.code == "missing_locator"
    with pytest.raises(Exception):
        validate_record(base(requirements=[{"requirement_id": "r", "category": "exclusion", "text": "t", "locator": {"quote": "t"},
                                            "machine_rule": {"fact": "x", "op": "bogus"}}]))
    supplier = party("supplier", "Fixture Digital Ltd", identifiers=[{"scheme": "LEI", "id": "213800FIXTUREDIGI007"}])
    assert lei_of(supplier) == "213800FIXTUREDIGI007" and lei_of(BUYER) is None


def test_cpv_codes_are_normalised_and_related_by_hierarchy():
    assert cpv_code("72253000-3") == "72253000" and cpv_code("7225") is None
    assert cpv_relation("72000000", "72253000") == "covers"
    assert cpv_relation("72250000", "72253000") == "covers"
    assert cpv_relation("72400000", "72253000") == "same-division"
    assert cpv_relation("55520000", "72253000") == "none"


def test_every_fixture_record_validates_against_the_registered_json_schema():
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-procurement-record-v1.json").read_text())
    validator = jsonschema.Draft7Validator(schema)
    records = all_fixture_records()
    assert len(records) >= 15
    for item in records:
        assert not list(validator.iter_errors(item)), item["notice_id"]
    broken = copy.deepcopy(records[0])
    broken["stage"] = "tender"
    assert list(validator.iter_errors(broken))


def test_schemas_register_in_the_schema_registry_and_validate_fixtures():
    conn = duckdb.connect()
    registry = SchemaRegistry(conn, clock=lambda: 1)
    registered = register_schemas(registry, principal_id="registry-admin", scopes={REGISTER_SCOPE})
    assert {r["name"] for r in registered} == {"procurement-record", "procurement-profile", "procurement-shortlist"}
    again = register_schemas(registry, principal_id="registry-admin", scopes={REGISTER_SCOPE})
    assert [r["module_id"] for r in again] == [r["module_id"] for r in registered]
    for item in all_fixture_records()[:5]:
        result = registry.validate_instance({"kind": "schema", "name": "procurement-record", "version": "^1.0.0"}, item,
                                            scopes={VALIDATE_SCOPE, READ_SCOPE})
        assert result["valid"], result["errors"]


def test_record_builder_normalises_titles():
    item = record("ted", "prior-information", "n", "p", "  Planned   works ", source_url="https://ted.europa.eu/x", buyer=BUYER)
    assert item["title"] == "Planned works" and item["unknowns"]
