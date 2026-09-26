"""Dossier inputs from acquired materials and filed facts (#1673).

Cases mirror live 2026-09-24 releases: CRM quarterly guidance ranges, ADBE
"Targets" tables that must not be read as the reported headline, and fiscal
fourth quarters reported only inside the annual figure.
"""

from __future__ import annotations

import duckdb

from src.domains.market.dossier_inputs import (
    build_dossier_inputs,
    headline_revenue,
    quarterly_revenue_from_facts,
    quarterly_revenue_guidance,
)
from src.domains.market.research import MarketResearchStore
from tests.unit.domains.test_market_research import NS, SCOPES

DAY = 86_400_000


def test_headline_prefers_same_line_total_and_skips_sub_lines_and_tables():
    text = "\n".join([
        "Subscription revenue was $6.39 billion, up 11%",
        "Adobe achieved record revenue of $6.76 billion in its third quarter",
        "Targets", "Total revenue", "$6.80 billion",
    ])
    headline = headline_revenue(text)
    assert headline["value"] == "6760000000"
    assert headline["tolerance"] == "5000000"
    assert text[headline["start"]:headline["end"]] == headline["text"]


def test_quarter_guidance_range_ignores_annual_range():
    assert quarterly_revenue_guidance(
        "Initiates third quarter FY27 revenue guidance of $11.42 billion to $11.5 billion, up 11% - 12% Y/Y"
    ) == {"low": "11420000000", "high": "11500000000", "range_text": "$11.42 billion to $11.5 billion"}
    assert quarterly_revenue_guidance("Raises full year FY27 revenue guidance to $46.1 billion to $46.4 billion") is None


def _fact(start, end, value, cls, public, rev):
    return {"canonical_concept": "revenue", "unit": "USD", "period_class": cls,
            "period": {"start_date": start, "end_date": end}, "value_lexical": value,
            "public_at_ms": public, "revision_id": rev}


def test_fourth_quarter_is_derived_from_annual_and_labeled():
    facts = [
        _fact("2025-02-01", "2025-04-30", "100", "quarter", 1, "q1"),
        _fact("2025-05-01", "2025-07-31", "110", "quarter", 2, "q2"),
        _fact("2025-08-01", "2025-10-31", "120", "quarter", 3, "q3"),
        _fact("2025-02-01", "2026-01-31", "460", "annual", 4, "fy"),
        _fact("2025-05-01", "2025-07-31", "111", "quarter", 9, "q2-restated"),
    ]
    rows = quarterly_revenue_from_facts(facts)
    assert [row["value"] for row in rows] == ["100", "110", "120", "130"]
    assert rows[1]["source_revision_id"] == "q2"  # as first filed
    assert rows[3]["derivation"] == "annual_minus_three_quarters"
    assert rows[3]["derived_from_revision_ids"] == ["fy", "q1", "q2", "q3"]


def test_dossier_compares_prior_guidance_and_keeps_claims_separate():
    q2_end, q3_end = 1_000 * DAY, 1_092 * DAY
    materials = {"materials": [
        {"material_id": "r2", "kind": "earnings_release", "licensing_status": "public", "published_at_ms": q2_end + 30 * DAY,
         "event_at_ms": q2_end + 30 * DAY, "reporting_period": "q2", "source_revision_id": "sec:r2"},
        {"material_id": "g2", "kind": "guidance", "licensing_status": "public", "derived_from_material_id": "r2",
         "published_at_ms": q2_end + 30 * DAY, "source_revision_id": "sec:r2",
         "statements": [{"text": "Initiates third quarter revenue guidance of $1.10 billion to $1.12 billion", "start": 0, "end": 70}]},
        {"material_id": "r3", "kind": "earnings_release", "licensing_status": "public", "published_at_ms": q3_end + 30 * DAY,
         "event_at_ms": q3_end + 30 * DAY, "reporting_period": "q3", "source_revision_id": "sec:r3"},
        {"material_id": "t", "kind": "earnings_call_transcript", "licensing_status": "unlicensed", "published_at_ms": 0,
         "document_locator": "unavailable:t", "source_revision_id": "unavailable:t"},
    ]}
    revenue = [
        {"period_end_ms": q2_end, "value": "1050000000", "public_at_ms": q2_end + 40 * DAY, "source_revision_id": "fact:q2"},
        {"period_end_ms": q3_end, "value": "1130000000", "public_at_ms": q3_end + 40 * DAY, "source_revision_id": "fact:q3"},
    ]
    texts = {"r2": "Revenue of $1.05 billion, up 9%", "r3": "Revenue of $1.14 billion, up 10%"}

    inputs = build_dossier_inputs(materials, quarterly_revenue=revenue, release_texts=texts)

    assert [c["status"] for c in inputs["headline_reconciliation"]] == ["consistent_at_stated_precision", "mismatch"]
    q3 = inputs["comparisons"][1]
    assert q3["guidance"]["outcome"] == "above_range" and q3["guidance"]["claim_status"] == "management_claim"
    assert inputs["unavailable_sources"] == ["earnings_call_transcript"]
    assert any(e["claim_kind"] == "management" for e in inputs["evidence"])
    assert any(e["stance"] == "contradicting" for e in inputs["evidence"])

    store = MarketResearchStore(duckdb.connect(":memory:"), now=lambda: q3_end + 60 * DAY)
    dossier = store.company_dossier(
        NS, issuer_id="issuer:x", artifact_id="dossier:x", version=1, statements=[],
        materials=inputs["materials"], comparisons=inputs["comparisons"], evidence=inputs["evidence"],
        cutoff_ms=q3_end + 60 * DAY, owner=None, principal_id="alice", scopes=SCOPES,
        segment_disclosures=inputs["segment_disclosures"], ownership=inputs["ownership"],
    )
    comparison = dossier["comparisons"][1]
    assert comparison["guidance_comparison"] == {"status": "available", "delta": 1130000000 - 1110000000}
    assert comparison["verified_calculation"]["delta"] == 80000000
    assert "pre_event_consensus_missing" in dossier["uncertainty"]["source_gaps"]
