"""Funding record contracts (#1763): instruments, money semantics, unknowns, awards."""

import json
from pathlib import Path

import jsonschema
import pytest

from src.kb.funding_records import (
    FundingRecordError,
    award_from_extraction,
    is_cash_equivalent,
    record,
    validate_record,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = {"source_url": "https://nlnet.nl/propose/", "authority": {"kind": "funder", "name": "NLnet"}}


def test_absent_values_are_listed_as_unknown_not_defaulted():
    call = record("nlnet", "call", "commonsfund", "Fund", **BASE)
    assert call["instrument"]["kinds"] == ["unknown"]
    assert {"status", "deadlines", "financial_terms.currency", "financial_terms.award_range",
            "financial_terms.eligible_costs", "financial_terms.co_financing", "instrument.kinds"} <= set(call["unknowns"])
    assert "financial_terms" not in call or call.get("financial_terms") in (None, {})


def test_financial_terms_distinguish_instruments_budget_bases_and_co_financing():
    loan = record("foerderdatenbank", "directory_entry", "Bund/KfW/x", "Kredit", source_url="https://www.foerderdatenbank.de/x.html",
                  authority={"kind": "directory", "name": "FDB"}, instrument={"kinds": ["loan"]},
                  financial_terms={"currency": "EUR", "repayable": True})
    grant = record("eu-ft", "call", "T-1", "Topic", source_url="https://ec.europa.eu/t", authority={"kind": "funder", "name": "EC"},
                   instrument={"kinds": ["grant"]}, financial_terms={
                       "currency": "EUR", "programme_budget": {"amount": "30000000", "basis": "topic-total", "expected_awards": 6},
                       "award_range": {"min": "4000000", "max": "5000000", "basis": "per-project"},
                       "funding_rate": {"max_percent": "100"}, "co_financing": {"required": False},
                       "eligible_costs": ["personnel", "equipment"], "reimbursement": "advance"})
    assert not is_cash_equivalent(loan) and is_cash_equivalent(grant)
    assert grant["financial_terms"]["programme_budget"]["basis"] == "topic-total"
    assert grant["financial_terms"]["award_range"]["basis"] == "per-project"
    with pytest.raises(FundingRecordError):
        record("nlnet", "call", "x", "x", **BASE, financial_terms={"award_range": {"max": 50000, "basis": "per-project"}, "currency": "EUR"})
    with pytest.raises(FundingRecordError, match="currency"):
        record("nlnet", "call", "x", "x", **BASE, financial_terms={"award_range": {"max": "50000", "basis": "per-project"}})
    with pytest.raises(FundingRecordError):
        record("nlnet", "call", "x", "x", **BASE, financial_terms={"currency": "EUR", "award_range": {"min": "9", "max": "1", "basis": "per-project"}})


def test_awards_programmes_and_directory_entries_are_not_opportunities():
    with pytest.raises(FundingRecordError) as exc:
        record("eu-ft", "award", "A-1", "Grant made", source_url="https://ec.europa.eu/a", authority={"kind": "funder", "name": "EC"},
               status={"asserted": "open"})
    assert exc.value.code == "not_an_opportunity"
    with pytest.raises(FundingRecordError):
        record("foerderdatenbank", "directory_entry", "x", "x", source_url="https://www.foerderdatenbank.de/x",
               authority={"kind": "directory", "name": "FDB"}, status={"asserted": "open"})
    with pytest.raises(FundingRecordError):
        record("exist", "programme", "x", "x", source_url="https://www.exist.de/x", authority={"kind": "funder", "name": "BMWE"},
               deadlines=[{"kind": "submission", "text": "31 January"}])
    award = award_from_extraction({"programme": "Horizon Europe", "beneficiary": "Org", "amount": "EUR 1.2m"},
                                  provider="eu-ft", provider_id="A-1", source_url="https://ec.europa.eu/a")
    assert award["record_kind"] == "award" and "status" not in award
    assert "EUR 1.2m" in award["financial_terms"]["notes"]


def test_deadlines_keep_original_text_and_require_timezone_for_instants():
    with pytest.raises(FundingRecordError):
        record("nlnet", "call", "x", "x", **BASE, deadlines=[{"kind": "submission", "text": "1 Oct", "instant": "2026-10-01T12:00:00+02:00"}])
    call = record("nlnet", "call", "x", "x", **BASE, deadlines=[{"kind": "submission", "text": "1 Oct 2026", "date": "2026-10-01"}])
    assert "deadlines.instant" in call["unknowns"]


def test_requirements_need_locators_and_private_state_cannot_leak():
    with pytest.raises(FundingRecordError) as exc:
        record("nlnet", "call", "x", "x", **BASE, requirements=[{"requirement_id": "r", "category": "licensing", "text": "Open"}])
    assert exc.value.code == "missing_locator"
    with pytest.raises(FundingRecordError) as exc:
        validate_record({**record("nlnet", "call", "x", "x", **BASE), "profile_id": "funding-profile:abc"})
    assert exc.value.code == "private_leak"


def test_json_schema_accepts_records_and_existing_award_schema_is_reused_not_reinterpreted():
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-funding-record-v1.json").read_text())
    jsonschema.validate(record("nlnet", "call", "x", "x", **BASE), schema)
    extraction = json.loads((ROOT / "config/extraction_schemas/eu-funding.json").read_text())
    assert set(extraction["structures"]) == {"award"}
