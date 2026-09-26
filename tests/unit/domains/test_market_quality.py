"""Market quality findings, quarantine filtering and bounded repair replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src.domains.market.actions import MarketCorporateActionStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.quality import MarketQualityError, MarketQualityStore
from tests.unit.domains.test_market_asof import (
    NAMESPACE,
    PRINCIPAL,
    SCOPES,
    T0,
    T1,
    bar,
    source_ref,
    seeded_market,
)

ROOT = Path(__file__).resolve().parents[3]


def put_two_provider_bars():
    conn, clock, _, prices, _ = seeded_market()
    first = prices.put_bar(
        NAMESPACE,
        bar(T0 + 100, close=100.0, retrieved_at_ms=T0, public_at_ms=T0),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    second_payload = bar(
        T0 + 100, close=110.0, retrieved_at_ms=T0, public_at_ms=T0
    )
    second_payload["provider"] = "second-fixture-provider"
    second_payload["provider_record_id"] = "second-provider-bar"
    second_payload["source_refs"][0]["source_ref_id"] = "src:second-provider"
    second_payload["source_refs"][0]["provider_object_id"] = "second-provider"
    second = prices.put_bar(
        NAMESPACE,
        second_payload,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    return conn, clock, prices, first, second


def assess(store: MarketQualityStore, *, request_key="provider-compare"):
    return store.assess_price_range(
        NAMESPACE,
        request_key,
        "listing:asof",
        interval="1d",
        start_ms=T0,
        end_ms=T0 + 5_000,
        publicly_available_by_ms=T0,
        acquired_by_ms=T0,
        stale_after_ms=1_000,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        source_disagreement_fraction=0.05,
    )


def test_quality_assessment_reports_source_disagreement_staleness_and_provenance():
    conn, _, _, first, second = put_two_provider_bars()
    store = MarketQualityStore(conn, now=lambda: T1)
    report = assess(store)

    rules = {item["rule"] for item in report["findings"]}
    assert report["status"] == "degraded"
    assert report["freshness"]["status"] == "stale"
    assert {"provider_price_disagreement", "stale_price_history", "session_coverage_unverified"} <= rules
    assert {
        item["revision_id"]
        for item in report["input_refs"]
        if item["kind"] == "price_bar"
    } == {
        first["revision_id"],
        second["revision_id"],
    }
    assert store.inspect_assessment(
        NAMESPACE,
        report["assessment_id"],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )["assessment_hash"] == report["assessment_hash"]
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-market-quality-assessment-v1.json"
            ).read_text()
        )
    ).validate(report)
    assert assess(store)["idempotent"] is True
    conn.close()


def test_quarantined_revision_is_hidden_from_history_and_released_with_audit():
    conn, _, prices, first, second = put_two_provider_bars()
    quality = MarketQualityStore(conn, now=lambda: T1)
    quarantine = quality.quarantine_revision(
        NAMESPACE,
        "price_bar",
        first["revision_id"],
        "provider disagreement under review",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        finding_ids=["finding:provider-price-disagreement"],
    )
    assert quality.quarantine_revision(
        NAMESPACE,
        "price_bar",
        first["revision_id"],
        "provider disagreement under review",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        finding_ids=["finding:provider-price-disagreement"],
    ) == {**quarantine, "idempotent": True}
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-market-quality-quarantine-v1.json"
            ).read_text()
        )
    ).validate(quarantine)
    selected = prices.get_bars(
        NAMESPACE,
        "listing:asof",
        start_ms=T0,
        end_ms=T0 + 5_000,
        acquired_by_ms=T0,
        publicly_available_by_ms=T0,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert [item["revision_id"] for item in selected] == [second["revision_id"]]
    listed = quality.list_quarantine(
        NAMESPACE, principal_id=PRINCIPAL, scopes=SCOPES
    )
    assert listed["items"][0]["status"] == "quarantined"
    after_quarantine = assess(quality, request_key="after-quarantine")
    assert after_quarantine["coverage"]["quarantined_rows"] == 1
    assert any(
        item["rule"] == "quarantined_source_input"
        for item in after_quarantine["findings"]
    )
    released = quality.release_quarantine(
        NAMESPACE,
        "price_bar",
        first["revision_id"],
        "source reconciliation passed",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert quality.release_quarantine(
        NAMESPACE,
        "price_bar",
        first["revision_id"],
        "source reconciliation passed",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    ) == {**released, "idempotent": True}
    assert len(
        prices.get_bars(
            NAMESPACE,
            "listing:asof",
            start_ms=T0,
            end_ms=T0 + 5_000,
            acquired_by_ms=T0,
            publicly_available_by_ms=T0,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    ) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM market_quality_quarantine_events"
    ).fetchone()[0] == 2
    conn.close()


def test_repair_plan_uses_existing_bounded_idempotent_backfill_runner():
    conn, _, _, _, _ = seeded_market()
    quality = MarketQualityStore(conn, now=lambda: T1)
    plan = quality.create_price_repair_plan(
        NAMESPACE,
        "repair-week",
        provider="fixture-provider",
        listing_id="listing:asof",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        budget={"max_requests_per_run": 2, "max_records_per_run": 10},
    )
    Draft7Validator(
        json.loads(
            (ROOT / "contracts/schemas/jsonschema/noesis-market-repair-plan-v1.json").read_text()
        )
    ).validate(plan)
    assert quality.create_price_repair_plan(
        NAMESPACE,
        "repair-week",
        provider="fixture-provider",
        listing_id="listing:asof",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        budget={"max_requests_per_run": 2, "max_records_per_run": 10},
    )["idempotent"]

    calls = []

    def fetch_page(**request):
        calls.append(request)
        suspect = bar(
            T0 + 100,
            close=101.0,
            retrieved_at_ms=T0,
            public_at_ms=T0,
        )
        suspect["currency"] = "EUR"
        valid = bar(
            T0 + 200,
            close=102.0,
            retrieved_at_ms=T0,
            public_at_ms=T0,
        )
        return {"bars": [suspect, valid], "next_cursor": None, "done": True}

    completed = quality.run_price_repair_plan(
        NAMESPACE,
        plan["plan_id"],
        fetch_page=fetch_page,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert completed["status"] == "complete"
    assert completed["result"]["status"] == "complete"
    assert completed["result"]["records_processed"] == 1
    assert completed["result"]["quarantined_records"] == 1
    candidates = quality.list_invalid_candidates(
        NAMESPACE,
        completed["checkpoint_request_key"],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert candidates["items"][0]["error_code"] == "currency_mismatch"
    assert "content" not in candidates["items"][0]
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-market-quality-candidate-list-v1.json"
            ).read_text()
        )
    ).validate(candidates)
    again = quality.run_price_repair_plan(
        NAMESPACE,
        plan["plan_id"],
        fetch_page=fetch_page,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert again["idempotent"] is True
    assert len(calls) == 1
    with pytest.raises(MarketQualityError) as exc:
        quality.create_price_repair_plan(
            NAMESPACE,
            "too-long",
            provider="fixture-provider",
            listing_id="listing:asof",
            interval="1d",
            start_date="2010-01-01",
            end_date="2025-01-01",
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    assert exc.value.code == "range_too_large"
    conn.close()


def test_repair_plan_recovers_from_provider_outage_and_resumes_partial_backfill():
    conn, _, _, prices, _ = seeded_market()
    quality = MarketQualityStore(conn, now=lambda: T1)
    plan = quality.create_price_repair_plan(
        NAMESPACE,
        "repair-resume-after-outage",
        provider="fixture-provider",
        listing_id="listing:asof",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        budget={"max_requests_per_run": 1, "max_records_per_run": 10},
    )
    calls = []

    def unavailable(**request):
        calls.append(request["cursor"])
        raise RuntimeError("fixture source unavailable")

    failed = quality.run_price_repair_plan(
        NAMESPACE,
        plan["plan_id"],
        fetch_page=unavailable,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert failed["status"] == "failed"
    assert failed["result"] == {"status": "failed", "error_code": "provider_error"}

    def first_page(**request):
        calls.append(request["cursor"])
        return {
            "bars": [
                bar(T0 + 100, close=100.0, retrieved_at_ms=T0, public_at_ms=T0)
            ],
            "next_cursor": "page:2",
            "done": False,
        }

    partial = quality.run_price_repair_plan(
        NAMESPACE,
        plan["plan_id"],
        fetch_page=first_page,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert partial["status"] == "paused"
    assert partial["result"]["records_processed"] == 1

    def final_page(**request):
        calls.append(request["cursor"])
        assert request["cursor"] == "page:2"
        return {
            "bars": [
                bar(T0 + 200, close=101.0, retrieved_at_ms=T0, public_at_ms=T0)
            ],
            "next_cursor": None,
            "done": True,
        }

    completed = quality.run_price_repair_plan(
        NAMESPACE,
        plan["plan_id"],
        fetch_page=final_page,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert completed["status"] == "complete"
    assert completed["result"]["records_processed"] == 2
    assert calls == [None, None, "page:2"]
    assert len(
        prices.get_bars(
            NAMESPACE,
            "listing:asof",
            start_ms=T0,
            end_ms=T0 + 5_000,
            acquired_by_ms=2_000_000_000_000,
            publicly_available_by_ms=T0,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    ) == 2
    conn.close()


def fact_payload(fact_id: str, accession: str, unit: str, value: str) -> dict:
    ref = source_ref(f"fact:{fact_id}", T0)
    return {
        "contract": "noesis-market-financial-fact-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "fact_observation_id": fact_id,
        "issuer_id": "issuer:asof",
        "revision_id": "provider-revision-is-replaced",
        "revision": 1,
        "filing_accession": accession,
        "filing_form": "10-K",
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "canonical_concept": "revenue",
        "statement": "income_statement",
        "mapping_status": "mapped",
        "context_id": f"context:{fact_id}",
        "context_id_kind": "companyfacts_composite_key",
        "unit": unit,
        "period": {
            "kind": "duration",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
        },
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_class": "annual",
        "value_lexical": value,
        "scale": 0,
        "decimals": -3,
        "filed_at_ms": T0,
        "accepted_at_ms": T0,
        "public_at_ms": T0,
        "retrieved_at_ms": T0,
        "source_document_revision_id": f"document-revision:{accession}",
        "source_locator": f"companyfacts/us-gaap/Revenues/{accession}",
        "provider": "fixture-provider",
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": T0,
        "record_hash": "ignored",
    }


def test_fact_unit_mismatch_is_reported_and_quarantine_filters_source_query():
    conn, _, _, _, _ = seeded_market()
    fact_store = MarketFinancialFactStore(conn, now=lambda: T0)
    facts = [
        fact_store.put_fact(
            NAMESPACE,
            fact_payload("fact:usd", "accession-usd", "USD", "100"),
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        ),
        fact_store.put_fact(
            NAMESPACE,
            fact_payload("fact:eur", "accession-eur", "EUR", "90"),
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        ),
    ]
    quality = MarketQualityStore(conn, now=lambda: T1)
    report = quality.assess_financial_facts(
        NAMESPACE,
        "revenue-unit-check",
        "issuer:asof",
        publicly_available_by_ms=T0,
        acquired_by_ms=T0,
        stale_after_ms=1_000,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    rules = {item["rule"] for item in report["findings"]}
    assert "financial_fact_unit_mismatch" in rules
    assert "stale_financial_facts" in rules
    quality.quarantine_revision(
        NAMESPACE,
        "financial_fact",
        facts[0]["revision_id"],
        "unit normalization required",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    selected = fact_store.get_facts(
        NAMESPACE,
        "issuer:asof",
        acquired_by_ms=T0,
        publicly_available_by_ms=T0,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        require_complete=True,
    )
    assert [item["revision_id"] for item in selected] == [facts[1]["revision_id"]]
    after_quarantine = quality.assess_financial_facts(
        NAMESPACE,
        "revenue-after-quarantine",
        "issuer:asof",
        publicly_available_by_ms=T0,
        acquired_by_ms=T0,
        stale_after_ms=1_000,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert after_quarantine["coverage"]["quarantined_rows"] == 1
    assert any(
        item["rule"] == "quarantined_source_input"
        for item in after_quarantine["findings"]
    )
    conn.close()


def test_quarantined_corporate_action_is_excluded_until_review_releases_it():
    conn, _, _, _, _ = seeded_market()
    actions = MarketCorporateActionStore(conn, now=lambda: T0)
    ref = source_ref("action:dividend", T0)
    action = actions.put_action(
        NAMESPACE,
        {
            "contract": "noesis-market-corporate-action-v1",
            "namespace": NAMESPACE,
            "owner": None,
            "action_id": "action:quality-dividend",
            "issuer_id": "issuer:asof",
            "security_id": "security:asof",
            "listing_id": "listing:asof",
            "related_security_id": None,
            "related_listing_id": None,
            "revision_id": "provider-revision-is-replaced",
            "revision": 1,
            "provider": "fixture-provider",
            "provider_record_id": "quality-dividend-1",
            "action_type": "cash_dividend",
            "status": "confirmed",
            "announced_at_ms": T0,
            "public_at_ms": T0,
            "effective_date": "2026-09-24",
            "ex_date": "2026-09-24",
            "record_date": None,
            "payable_date": None,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "cash_amount": "0.2",
            "cash_amount_basis": "per_share_before_action",
            "distribution_type": "regular",
            "currency": "USD",
            "prior_revision_id": None,
            "source_refs": [ref],
            "recorded_at_ms": T0,
            "record_hash": "ignored",
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    quality = MarketQualityStore(conn, now=lambda: T1)
    quality.quarantine_revision(
        NAMESPACE,
        "corporate_action",
        action["revision_id"],
        "provider source disagreement",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    def read_actions():
        return actions.get_actions(
            NAMESPACE,
            "security:asof",
            acquired_by_ms=T0,
            publicly_available_by_ms=T0,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )

    assert read_actions() == []
    quality.release_quarantine(
        NAMESPACE,
        "corporate_action",
        action["revision_id"],
        "reconciled against provider filing",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert [item["revision_id"] for item in read_actions()] == [action["revision_id"]]
    conn.close()
