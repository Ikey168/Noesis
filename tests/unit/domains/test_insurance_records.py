"""Insurer-report, supervisory-indicator and catastrophe-loss-estimate records (#2230, IN02)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import jsonschema
import pytest

from src.domains.market.insurance import (
    CONTRACT,
    InsuranceError,
    InsuranceStore,
    cutoffs,
    decimal_text,
    lei_valid,
    record_id,
    schema,
    validate_record,
)

NS = "market-insurance"
LEI = "529900FIKTIVAVERS084"


def ms(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp() * 1000)


def estimate(value: str, published: str, *, provider: str = "florida-oir-claims", **extra) -> dict:
    return {
        "contract": CONTRACT,
        "kind": "catastrophe_loss_estimate",
        "source": {
            "provider": provider,
            "source_id": "fictional-storm-2024|insured|estimated_insured_losses",
            "url": "https://floir.com/fixture/claims.csv",
            "locator": {"row": 2},
        },
        "publication_date": published,
        "publisher": "Florida Office of Insurance Regulation",
        "event": {"name": "Hurricane Fiktiva", "identifiers": {"nhc_storm_id": "AL992024"}, "begin_date": "2024-09-26"},
        "estimate_type": "insured",
        "measure": "estimated insured losses",
        "value": value,
        "unit": "USD",
        "currency": "USD",
        **extra,
    }


def indicator(value: str | None, release: str, *, marker: str | None = None) -> dict:
    return {
        "contract": CONTRACT,
        "kind": "supervisory_indicator",
        "source": {
            "provider": "eiopa-insurance-statistics",
            "source_id": "premiums|DE|motor|2024",
            "url": f"https://www.eiopa.europa.eu/fixture/{release}.csv",
        },
        "publication_date": release,
        "publisher": "EIOPA",
        "dataset": "Premiums, claims and expenses by line of business",
        "indicator": "gross_written_premiums",
        "indicator_label": "Gross written premiums",
        "dimensions": {"country": "DE", "line_of_business": "Motor vehicle liability insurance"},
        "period": {"reference_year": 2024},
        "value": value,
        "marker": marker,
        "unit": "EUR million",
        "currency": "EUR",
        "release": release,
    }


def report(sha: str, published: str, figures: list[dict]) -> dict:
    return {
        "contract": CONTRACT,
        "kind": "insurer_report",
        "source": {"provider": "insurer-sfcr", "source_id": f"{LEI}|sfcr|2024", "url": "https://example.org/sfcr.pdf"},
        "publication_date": published,
        "insurer": {"name": "Fiktiva Versicherung AG", "lei": LEI, "country": "DE", "reporting_level": "group"},
        "report_type": "sfcr",
        "reporting_year": 2024,
        "document": {"url": "https://example.org/sfcr.pdf", "sha256": sha, "media_type": "application/pdf"},
        "figures": figures,
    }


SCR = {
    "template": "S.25.01.21",
    "row": "R0220",
    "column": "C0100",
    "label": "Solvency capital requirement",
    "status": "reported",
    "value": "1234.5",
    "unit": "EUR thousand",
    "currency": "EUR",
}


@pytest.fixture
def store():
    return InsuranceStore(duckdb.connect(":memory:"))


def test_every_kind_validates_against_the_json_schema():
    validator = jsonschema.Draft7Validator(schema())
    unknown = {**SCR, "row": "R0100", "status": "unknown", "value": None, "reason": "cell not found"}
    records = [
        estimate("1000000", "2024-10-15", range={"low": "900000", "high": "1100000"}),
        indicator("123.4", "2025-06-30"),
        indicator(None, "2025-06-30", marker="c"),
        report("a" * 64, "2025-04-30", [SCR, unknown]),
        {
            "contract": CONTRACT,
            "kind": "publication_reference",
            "source": {"provider": "naic-public", "source_id": "naic-pc-2024", "url": "https://content.naic.org/x.pdf"},
            "publication_date": "2025-05-01",
            "publisher": "NAIC",
            "title": "2024 Market Share Reports for Property/Casualty Groups and Companies",
            "period": "2024",
        },
    ]
    for item in records:
        normalized = validate_record(item)
        assert not list(validator.iter_errors(json.loads(json.dumps(normalized))))


def test_licence_decisions_refuse_excluded_and_restrict_metadata_only_publishers():
    with pytest.raises(InsuranceError) as excluded:
        validate_record(estimate("1", "2024-10-15", provider="perils"))
    assert excluded.value.code == "licence_excluded"
    with pytest.raises(InsuranceError) as metadata:
        validate_record(estimate("1", "2024-10-15", provider="swiss-re-sigma"))
    assert metadata.value.code == "metadata_only"
    stored = validate_record(estimate("1", "2024-10-15"))
    assert stored["source"]["licence"]["decision"] == "in-scope"
    assert "Florida Office of Insurance Regulation" in stored["source"]["licence"]["attribution"]


def test_markers_are_never_zero_and_figures_are_never_estimated():
    with pytest.raises(InsuranceError):
        validate_record(indicator(None, "2025-06-30"))
    confidential = validate_record(indicator(None, "2025-06-30", marker="c"))
    assert confidential["value"] is None and confidential["marker"] == "c"
    with pytest.raises(InsuranceError) as estimated:
        validate_record(report("a" * 64, "2025-04-30", [{**SCR, "status": "unknown", "value": "1"}]))
    assert estimated.value.code == "estimated_value"
    with pytest.raises(InsuranceError) as forbidden:
        validate_record({**indicator("1", "2025-06-30"), "derived_ratio": "2.1"})
    assert forbidden.value.code == "excluded_field"


def test_an_estimate_revised_three_times_keeps_every_publication(store):
    for day, value in (("2024-10-15", "1000000"), ("2024-11-15", "1500000"), ("2025-01-15", "1450000")):
        store.apply(NS, [estimate(value, day)], run_id=f"run:{day}", observed_at_ms=ms(day, 18))
    rid = record_id(NS, "florida-oir-claims", "fictional-storm-2024|insured|estimated_insured_losses")
    history = store.history(NS, rid)
    assert [h["record"]["value"] for h in history] == ["1000000", "1500000", "1450000"]
    assert [h["change"] for h in history] == ["new", "revised", "revised"]
    # As of a date between publications, the revision then in force; the day itself counts at its end.
    between = store.visible(NS, public_cutoff_ms=cutoffs("2024-11-15")["publicly_available_by_ms"])
    assert between[0]["record"]["value"] == "1500000" and between[0]["revisions_on_record"] == 2
    before = store.visible(NS, public_cutoff_ms=cutoffs("2024-11-14")["publicly_available_by_ms"])
    assert before[0]["record"]["value"] == "1000000"
    # Re-acquiring the same publication adds nothing; an older publication arriving late is history.
    assert store.apply(NS, [estimate("1450000", "2025-01-15")], run_id="again", observed_at_ms=ms("2025-02-01"))[
        "unchanged"
    ] == 1
    late = store.apply(NS, [estimate("1200000", "2024-10-30")], run_id="late", observed_at_ms=ms("2025-02-02"))
    assert late["history"] == 1
    assert store.visible(NS)[0]["record"]["value"] == "1450000"


def test_a_reversion_is_a_new_revision_and_an_unchanged_release_adds_none(store):
    store.apply(NS, [indicator("100", "2025-06-30")], run_id="r1", observed_at_ms=ms("2025-07-01"))
    assert store.apply(NS, [indicator("100", "2025-12-31")], run_id="r2", observed_at_ms=ms("2026-01-02"))[
        "unchanged"
    ] == 1
    store.apply(NS, [indicator("101", "2026-06-30")], run_id="r3", observed_at_ms=ms("2026-07-01"))
    store.apply(NS, [indicator("100", "2026-12-31")], run_id="r4", observed_at_ms=ms("2027-01-02"))
    rid = record_id(NS, "eiopa-insurance-statistics", "premiums|DE|motor|2024")
    assert [h["record"]["value"] for h in store.history(NS, rid)] == ["100", "101", "100"]


def test_a_corrected_sfcr_adds_a_revision_without_deleting_the_earlier_report(store):
    store.apply(NS, [report("a" * 64, "2025-04-30", [SCR])], run_id="r1", observed_at_ms=ms("2025-05-01"))
    corrected = report("b" * 64, "2025-06-10", [{**SCR, "value": "1240.0"}])
    corrected["corrects"] = "a" * 64
    store.apply(NS, [corrected], run_id="r2", observed_at_ms=ms("2025-06-11"))
    rid = record_id(NS, "insurer-sfcr", f"{LEI}|sfcr|2024")
    history = store.history(NS, rid)
    assert [h["record"]["document"]["sha256"] for h in history] == ["a" * 64, "b" * 64]
    assert history[0]["citation"]["licence"]["decision"] == "in-scope"


def test_identifiers_and_numbers_as_published():
    assert lei_valid(LEI) and not lei_valid(LEI[:-1] + "0")
    assert decimal_text("1.234,5", decimal=",") == "1234.5"
    assert decimal_text("(12)") == "-12"
    assert decimal_text("n/a") is None
    with pytest.raises(InsuranceError):
        validate_record(report("a" * 64, "2025-04-30", [SCR]) | {"insurer": {"name": "X", "lei": "BAD"}})
