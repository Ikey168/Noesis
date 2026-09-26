from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.capabilities import MarketCapabilityService
from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.research import (
    ACCEPTANCE_REVIEW_CRITERIA,
    RESEARCH_READ_SCOPE,
    RESEARCH_WRITE_SCOPE,
    MarketResearchError,
    MarketResearchStore,
    verify_market_brief_export,
)
from src.evidence_bundle import verify_bundle

ROOT = Path(__file__).resolve().parents[3]
NS = "market:research-fixture"
SCOPES = {RESEARCH_READ_SCOPE, RESEARCH_WRITE_SCOPE, "namespace:market:research-fixture:read", "namespace:market:research-fixture:write"}


def test_materials_dossier_industry_sizing_and_driver_artifacts_preserve_gaps():
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1000)
    materials = store.save_materials(
        NS,
        issuer_id="issuer:fixture",
        artifact_id="materials:fixture",
        version=1,
        cutoff_ms=900,
        materials=[
            {"material_id": "release:1", "kind": "earnings_release", "published_at_ms": 100, "event_at_ms": 200, "document_locator": "doc://release", "source_revision_id": "doc-rev-1", "licensing_status": "public"},
            {"material_id": "estimate:1", "kind": "estimate", "published_at_ms": 150, "event_at_ms": 200, "document_locator": "doc://estimate", "source_revision_id": "estimate-rev-1", "licensing_status": "licensed"},
            {"material_id": "transcript:private", "kind": "transcript", "published_at_ms": 160, "document_locator": "doc://private", "source_revision_id": "transcript-rev-1", "licensing_status": "private"},
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert materials["coverage"] == {"total": 3, "available": 2, "unavailable": 1}
    dossier = store.company_dossier(
        NS,
        issuer_id="issuer:fixture",
        artifact_id="dossier:fixture",
        version=1,
        statements=[{"period": "2025-Q4", "revenue": 120}],
        materials=materials["materials"],
        comparisons=[{"metric": "revenue", "actual": 120, "prior": 100, "event_at_ms": 200, "consensus": {"value": 110, "public_at_ms": 150}}],
        evidence=[{"claim": "growth", "claim_kind": "management", "stance": "supporting", "source_span": "release:1#growth", "source_revision_id": "doc-rev-1"}, {"claim": "margin", "stance": "contradicting", "source_span": "filing:1#margin", "source_revision_id": "filing-rev-1"}],
        cutoff_ms=900,
        segment_disclosures=[
            {"segment_id": "cloud", "period": "2025-Q4", "revenue": 70, "public_at_ms": 180, "source_revision_id": "segment-rev-1"},
            {"segment_id": "future-secret-segment", "public_at_ms": 901, "source_revision_id": "future-segment-rev"},
            {"segment_id": "undated-segment", "revenue": 999, "source_revision_id": "undated-segment-rev"},
        ],
        ownership=[
            {"holder_id": "holder:1", "as_of_ms": 200, "shares": 50, "public_at_ms": 250, "source_revision_id": "ownership-rev-1"},
            {"holder_id": "future-holder-secret", "as_of_ms": 901, "shares": 100, "public_at_ms": 300, "source_revision_id": "future-ownership-rev"},
        ],
        dated_peers=[{"peer_issuer_id": "issuer:peer", "as_of_ms": 400, "multiple": 8, "public_at_ms": 410, "source_revision_id": "peer-rev-1"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert dossier["comparisons"][0]["verified_calculation"]["delta"] == 20
    assert dossier["comparisons"][0]["consensus_comparison"]["status"] == "available"
    assert dossier["contradicting_source_spans"]
    assert [row["segment_id"] for row in dossier["segment_disclosures"]] == ["cloud"]
    assert [row["holder_id"] for row in dossier["ownership"]] == ["holder:1"]
    assert dossier["dated_peers"][0]["peer_issuer_id"] == "issuer:peer"
    assert dossier["source_coverage"]["segment_disclosures"]["excluded"] == {"after_cutoff": 1, "missing_publication_clock": 1}
    assert "segment-rev-1" in dossier["input_manifest"]["source_revision_ids"]
    assert "ownership-rev-1" in dossier["input_manifest"]["source_revision_ids"]
    assert "peer-rev-1" in dossier["input_manifest"]["source_revision_ids"]
    stored_input = conn.execute("SELECT input_json FROM market_research_artifacts WHERE artifact_id='dossier:fixture'").fetchone()[0]
    assert "future-secret-segment" not in stored_input
    assert "future-holder-secret" not in stored_input

    industry = store.industry_model(
        NS,
        artifact_id="industry:fixture",
        version=1,
        profiles=[{"company_id": "issuer:fixture", "industry": "software"}],
        relationships=[
            {"relationship_id": "r1", "relationship_type": "supplier", "subject_id": "issuer:fixture", "object_id": "company:supplier", "evidence_kind": "asserted", "valid_from_ms": 0, "stance": "supports"},
            {"relationship_id": "r2", "relationship_type": "supplier", "subject_id": "issuer:fixture", "object_id": "company:supplier", "evidence_kind": "extracted", "valid_from_ms": 0, "stance": "contradicts"},
        ],
        as_of_ms=100,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert industry["contradictions"]

    sizing = store.market_sizing(
        NS,
        artifact_id="sizing:fixture",
        version=1,
        model_type="bottom_up",
        segments=[{"segment_id": "us-enterprise", "geography": "US", "customer_segment": "enterprise", "unit": "customer-year", "period": "2026", "customers": 1000, "annual_price": 10, "adoption": 0.5, "capture": 0.2, "value_kind": "estimate"}],
        scenarios={"base": {"multiplier": 1}, "downside": {"multiplier": 0.8}},
        sensitivity=[{"parameter": "adoption", "low": 0.2, "high": 0.8}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert sizing["totals"]["tam"] == 10000
    assert sizing["scenarios"]["downside"]["som"] < sizing["totals"]["som"]
    with pytest.raises(MarketResearchError, match="overlap"):
        store.market_sizing(
            NS,
            artifact_id="sizing:bad",
            version=1,
            model_type="top_down",
            segments=[
                {"segment_id": "a", "overlap_group": "all", "geography": "US", "customer_segment": "all", "unit": "year", "period": "2026", "market_size": 10},
                {"segment_id": "b", "overlap_group": "all", "geography": "US", "customer_segment": "all", "unit": "year", "period": "2026", "market_size": 10},
            ],
            scenarios=None,
            sensitivity=None,
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )

    drivers = store.driver_hypotheses(
        NS,
        artifact_id="drivers:fixture",
        version=1,
        hypotheses=[{"hypothesis_id": "h1", "statement": "Demand increases adoption", "driver_types": ["demand", "technology"], "time_horizon": "2y", "alternatives": ["pricing"], "evidence": [{"stance": "supporting", "source_revision_ids": ["policy:1"]}, {"stance": "contradicting", "source_revision_ids": ["filing:2"]}]}],
        as_of_ms=900,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert drivers["transmission"][0]["status"] == "descriptive_association"
    for result, schema in ((materials, "noesis-market-materials-v1.json"), (dossier, "noesis-market-company-dossier-v1.json"), (industry, "noesis-market-industry-model-v1.json"), (sizing, "noesis-market-sizing-v1.json"), (drivers, "noesis-market-driver-hypotheses-v1.json")):
        Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema" / schema).read_text())).validate(result)
    conn.close()


def test_company_dossier_capability_forwards_cutoff_linked_research_inputs():
    conn = duckdb.connect(":memory:")
    result = MarketCapabilityService(conn).invoke(
        "build_market_company_dossier",
        {
            "namespace": NS,
            "issuer_id": "issuer:fixture",
            "artifact_id": "dossier:capability",
            "version": 1,
            "statements": [],
            "materials": [],
            "comparisons": [],
            "evidence": [],
            "cutoff_ms": 500,
            "segment_disclosures": [{"segment_id": "cloud", "public_at_ms": 400, "source_revision_id": "segment:1"}],
            "ownership": [{"holder_id": "holder:1", "public_at_ms": 450, "as_of_ms": 440, "source_revision_id": "ownership:1"}],
            "dated_peers": [{"peer_issuer_id": "issuer:peer", "public_at_ms": 470, "as_of_ms": 460, "source_revision_id": "peer:1"}],
            "headline_reconciliation": [{"material_id": "release:1", "status": "consistent_at_stated_precision"}],
            "input_gaps": [{"code": "analyst_consensus_unavailable", "kind": "analyst_consensus"}],
            "material_change_history": [{"material_id": "release:2", "corrects_material_id": "release:1"}],
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result["ok"]
    dossier = result["result"]
    assert len(dossier["segment_disclosures"]) == 1
    assert len(dossier["ownership"]) == 1
    assert len(dossier["dated_peers"]) == 1
    assert dossier["headline_reconciliation"][0]["status"] == "consistent_at_stated_precision"
    assert dossier["uncertainty"]["gap_details"][0]["code"] == "analyst_consensus_unavailable"
    assert dossier["material_change_history"][0]["corrects_material_id"] == "release:1"
    conn.close()


def test_thesis_review_brief_delivery_and_five_company_journey_are_replayable():
    conn = duckdb.connect(":memory:")
    clock = {"now": 1000}
    store = MarketResearchStore(conn, now=lambda: clock["now"])
    thesis = store.save_thesis(
        NS,
        thesis_id="thesis:fixture",
        version=1,
        question="Will demand grow?",
        thesis="Demand will grow",
        alternatives=["pricing pressure"],
        catalysts=["new product"],
        horizon="2026",
        assumptions=["no recession"],
        evidence=[{"stance": "supporting", "source_revision_ids": ["e1"]}],
        falsification_conditions=[{"condition": "retention falls", "threshold": 0.5}],
        watch_ids=["anomaly-watch:1"],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    review = store.review_thesis(NS, thesis_id="thesis:fixture", base_version=1, evidence=[{"stance": "contradicting", "source_revision_ids": ["e2"]}], proposed_revision="revise demand assumption", owner="alice", principal_id="alice", scopes=SCOPES)
    assert review["review_state"] == "contradicting" and not review["conclusion_overwritten"]
    brief = store.generate_brief(NS, report_id="brief:fixture", version=1, title="Fixture market brief", sections=[{"heading": "Company", "body": "Derived result: 1"}], cutoff_ms=900, formula_versions=["formula:1"], assumptions=["fixture"], source_locators=[{"locator": "doc://release", "source_revision_id": "doc-rev-1"}], charts=[{"chart_id": "chart:valuation", "chart_type": "line", "title": "Valuation", "series": [{"label": "fixture", "points": [[1, 2]]}], "source_revision_ids": ["doc-rev-1"]}], artifact_refs=[{"artifact_id": "dossier:fixture", "version": 1}], owner="alice", principal_id="alice", scopes=SCOPES)
    failed = store.deliver_brief(NS, report_id="brief:fixture", version=1, subscriber_id="alice", delivery_outcome="failed", retry_delay_ms=10, cooldown_ms=100, owner="alice", principal_id="alice", scopes=SCOPES)
    assert failed["status"] == "retrying"
    replay_delivery = store.deliver_brief(NS, report_id="brief:fixture", version=1, subscriber_id="alice", delivery_outcome="failed", retry_delay_ms=10, cooldown_ms=100, owner="alice", principal_id="alice", scopes=SCOPES)
    assert replay_delivery["deduplicated"]
    clock["now"] = failed["next_attempt_ms"]
    delivered = store.deliver_brief(NS, report_id="brief:fixture", version=1, subscriber_id="alice", delivery_outcome="delivered", retry_delay_ms=10, cooldown_ms=100, owner="alice", principal_id="alice", scopes=SCOPES)
    assert delivered["status"] == "delivered" and delivered["attempts"] == 2
    replay_delivered = store.deliver_brief(NS, report_id="brief:fixture", version=1, subscriber_id="alice", delivery_outcome="delivered", retry_delay_ms=10, cooldown_ms=100, owner="alice", principal_id="alice", scopes=SCOPES)
    assert replay_delivered["deduplicated"]
    exported = store.export_brief(NS, report_id="brief:fixture", version=1, output_format="markdown", external=False, owner="alice", principal_id="alice", scopes=SCOPES)
    assert exported["format"] == "markdown" and exported["rights"]["status"] == "unverified" and "Fixture market brief" in exported["payload"]
    assert verify_market_brief_export(exported)["status"] == "verified_consistency"
    tampered_export = {**exported, "payload": "Changed after export"}
    assert verify_market_brief_export(tampered_export)["status"] == "payload_hash_mismatch"
    json_export = store.export_brief(NS, report_id="brief:fixture", version=1, output_format="json", external=False, owner="alice", principal_id="alice", scopes=SCOPES)
    assert verify_market_brief_export(json_export)["valid"] is True
    schedule = store.schedule_brief(NS, schedule_id="schedule:fixture", report_id="brief:scheduled", cadence="daily", next_due_ms=900, request={"version": 1, "title": "Scheduled", "sections": [{"heading": "One", "body": "Two"}], "cutoff_ms": 900, "formula_versions": ["formula:1"], "assumptions": [], "source_locators": []}, owner="alice", principal_id="alice", scopes=SCOPES)
    scheduled = store.run_due_schedules(NS, due_at_ms=900, limit=10, owner="alice", principal_id="alice", scopes=SCOPES)
    assert schedule["status"] == "active" and scheduled["processed"] == 1
    scheduled_report = store.inspect(NS, "brief:scheduled", 1, principal_id="alice", scopes=SCOPES)
    assert scheduled_report["record_hash"] == scheduled["receipts"][0]["record_hash"]
    assert scheduled_report["cutoff_ms"] == 900
    scheduled_export = store.export_brief(NS, report_id="brief:scheduled", version=1, output_format="markdown", external=False, owner="alice", principal_id="alice", scopes=SCOPES)
    assert "# Scheduled" in scheduled_export["payload"]
    assert verify_market_brief_export(scheduled_export)["valid"] is True
    scheduled_external = store.export_brief(NS, report_id="brief:scheduled", version=1, output_format="json", external=True, owner="alice", principal_id="alice", scopes=SCOPES)
    assert scheduled_external["rights"]["export_withheld"] is True
    assert scheduled_external["payload"] is None
    replay_schedule = store.run_due_schedules(NS, due_at_ms=900, limit=10, owner="alice", principal_id="alice", scopes=SCOPES)
    assert replay_schedule["processed"] == 0
    journey = store.acceptance_journey(NS, journey_id="journey:fixture", companies=[{"company_id": f"issuer:{i}", "performance": i / 10, "valuation_change": i / 100, "evidence": [{"stance": "supporting"}]} for i in range(5)], macro_scenario={"series_id": "fred:CPI", "shock": 0.01}, cutoff_ms=900, owner="alice", principal_id="alice", scopes=SCOPES)
    assert len(journey["companies"]) == 5 and journey["analyst_review"]["status"] == "pending_human_review"
    assert brief["charts"][0]["source_revision_ids"] == ["doc-rev-1"] and brief["artifact_refs"][0]["artifact_id"] == "dossier:fixture"
    for result, schema in ((thesis, "noesis-market-thesis-v1.json"), (review, "noesis-market-thesis-review-v1.json"), (brief, "noesis-market-brief-v1.json"), (exported, "noesis-market-brief-export-v1.json"), (journey, "noesis-market-acceptance-journey-v1.json")):
        Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema" / schema).read_text())).validate(result)
    Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-brief-schedule-v1.json").read_text())).validate(schedule)
    Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-brief-schedule-run-v1.json").read_text())).validate(scheduled)
    conn.close()


def test_market_brief_chart_has_text_alternative_in_markdown_and_creation_export(monkeypatch):
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1000)
    brief = store.generate_brief(
        NS,
        report_id="brief:accessible-chart",
        version=1,
        title="Accessible chart fixture",
        sections=[{"heading": "Results", "body": "Revenue increased."}],
        cutoff_ms=900,
        formula_versions=["formula:fixture-v1"],
        assumptions=[],
        source_locators=[],
        charts=[
            {
                "chart_id": "chart:revenue",
                "chart_type": "bar",
                "title": "Revenue by period",
                "series": [{"label": "Actual", "points": [["2025-Q4", 12.5]]}],
                "source_revision_ids": ["filing-revision-1"],
            }
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    description = brief["charts"][0]["accessible_description"]
    assert "2025-Q4" in description and "12.5" in description
    assert description in brief["formats"]["markdown"]

    captured = {}

    def render_report(package, *, output_format, locale):
        captured.update(package=package, output_format=output_format, locale=locale)
        return {"content": b"fixture docx", "receipt": {"fixture": True}}

    monkeypatch.setattr("src.kb.citeproc_export.render_report", render_report)
    exported = store.export_brief(
        NS,
        report_id="brief:accessible-chart",
        version=1,
        output_format="docx",
        external=False,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert exported["payload"]["bytes_b64"] == "Zml4dHVyZSBkb2N4"
    assert captured["output_format"] == "docx"
    chart_section = captured["package"]["report"]["content"]["sections"][-1]
    assert chart_section["title"] == "Charts and text alternatives"
    assert description in chart_section["assertions"][0]["text"]
    assert chart_section["assertions"][0]["dependencies"] == ["filing-revision-1"]
    conn.close()


def test_acceptance_journey_review_requires_complete_human_attestation_and_is_immutable():
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1000)
    journey = store.acceptance_journey(
        NS,
        journey_id="journey:reviewed",
        companies=[
            {
                "company_id": f"issuer:{index}",
                "performance": index / 10,
                "valuation_change": index / 100,
                "source_revision_ids": [f"price:{index}"],
                "formula_versions": ["return:v1"],
                "evidence": [{"stance": "supporting"}, {"stance": "contradicting"}],
            }
            for index in range(5)
        ],
        macro_scenario={"series_id": "ecb:EXR", "shock": 0.01},
        cutoff_ms=900,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    criteria = [
        {"criterion_id": criterion_id, "outcome": "passed", "notes": "Reviewed in the fixture."}
        for criterion_id in ACCEPTANCE_REVIEW_CRITERIA
    ]
    with pytest.raises(MarketResearchError) as not_human:
        store.review_acceptance_journey(
            NS,
            journey_id="journey:reviewed",
            review_version=1,
            reviewer_id="alice",
            reviewed_at_ms=1000,
            decision="accepted",
            criteria=criteria,
            notes="Fixture invocation without a human attestation.",
            live_evidence_refs=[],
            human_attestation=False,
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert not_human.value.code == "human_review_required"

    result = MarketCapabilityService(conn).invoke(
        "review_market_acceptance_journey",
        {
            "namespace": NS,
            "journey_id": "journey:reviewed",
            "review_version": 1,
            "reviewer_id": "alice",
            "reviewed_at_ms": 1000,
            "decision": "accepted",
            "criteria": criteria,
            "notes": "Fixture human-attestation path; not a real analyst sign-off.",
            "live_evidence_refs": ["market-live-sec-materials-v1"],
            "human_attestation": True,
            "owner": "alice",
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result["ok"] is True
    review = result["result"]
    assert review["contract"] == "noesis-market-acceptance-review-v1"
    assert review["journey_record_hash"] == journey["record_hash"]
    assert review["decision"] == "accepted"
    assert len(review["criteria"]) == len(ACCEPTANCE_REVIEW_CRITERIA)
    assert review["human_attestation"] is True
    replay = store.review_acceptance_journey(
        NS,
        journey_id="journey:reviewed",
        review_version=1,
        reviewer_id="alice",
        reviewed_at_ms=1000,
        decision="accepted",
        criteria=criteria,
        notes="Fixture human-attestation path; not a real analyst sign-off.",
        live_evidence_refs=["market-live-sec-materials-v1"],
        human_attestation=True,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert replay["idempotent"] is True and replay["record_hash"] == review["record_hash"]
    unchanged = store.inspect(NS, "journey:reviewed", 1, principal_id="alice", scopes=SCOPES)
    assert unchanged["analyst_review"]["status"] == "pending_human_review"
    Draft7Validator(
        json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-acceptance-review-v1.json").read_text())
    ).validate(review)
    conn.close()


def test_external_brief_export_is_withheld_when_any_source_rights_are_unverified():
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1000)
    store.generate_brief(
        NS,
        report_id="brief:external-rights",
        version=1,
        title="Evidence-linked brief",
        sections=[{"heading": "Results", "body": "Derived content"}],
        cutoff_ms=900,
        formula_versions=[],
        assumptions=[],
        source_locators=[
            {"locator": "https://example.org/filing", "source_revision_id": "filing:rev-1"}
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )

    exported = store.export_brief(
        NS,
        report_id="brief:external-rights",
        version=1,
        output_format="markdown",
        external=True,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )

    assert exported["rights"]["status"] == "withheld"
    assert exported["rights"]["export_withheld"] is True
    assert exported["payload"] is None
    assert any("rights could not be verified" in item for item in exported["limitations"])
    assert verify_market_brief_export(exported)["status"] == "payload_withheld"
    Draft7Validator(
        json.loads(
            (ROOT / "contracts/schemas/jsonschema/noesis-market-brief-export-v1.json").read_text()
        )
    ).validate(exported)
    conn.close()


def test_brief_composes_pinned_research_artifacts_cutoff_safely():
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1000)

    def ref(name):
        return {
            "source_ref_id": f"source:{name}",
            "provider": "fixture-provider",
            "provider_object_id": name,
            "source_revision_id": f"{name}:revision-1",
            "public_at_ms": 100,
            "source_snapshot_id": f"snapshot:{name}",
            "source_url": f"https://fixture.example/{name}",
            "retrieved_at_ms": 200,
            "content_hash": hashlib.sha256(name.encode()).hexdigest(),
            "license_id": "fixture-license",
            "entitlement_id": "fixture-entitlement",
        }

    materials = store.save_materials(
        NS,
        issuer_id="issuer:fixture",
        artifact_id="materials:compose",
        version=1,
        materials=[
            {"material_id": "release", "kind": "earnings_release", "published_at_ms": 100, "event_at_ms": 150, "document_locator": "https://fixture.example/release", "source_revision_id": "release:revision-1", "source_refs": [ref("release")], "licensing_status": "public"},
            {"material_id": "restricted", "kind": "transcript", "published_at_ms": 100, "event_at_ms": 150, "document_locator": "https://fixture.example/restricted-transcript", "source_revision_id": "private-transcript-rev", "source_refs": [ref("private-transcript")], "licensing_status": "private"},
        ],
        cutoff_ms=900,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    dossier = store.company_dossier(
        NS,
        issuer_id="issuer:fixture",
        artifact_id="dossier:compose",
        version=1,
        statements=[{"period": "2025-FY", "revenue": 120}],
        materials=materials["materials"],
        comparisons=[{"metric": "revenue", "actual": 120, "prior": 100, "source_revision_id": "comparison-rev"}],
        evidence=[{"claim": "management outlook", "claim_kind": "management", "stance": "supporting", "source_span": "release#outlook", "source_revision_id": "public-evidence-rev"}],
        segment_disclosures=[],
        ownership=[],
        dated_peers=[],
        cutoff_ms=900,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    industry = store.industry_model(
        NS,
        artifact_id="industry:compose",
        version=1,
        profiles=[{"company_id": "issuer:fixture", "industry": "software"}],
        relationships=[{"relationship_id": "supplier", "relationship_type": "supplier", "subject_id": "issuer:fixture", "object_id": "company:supplier", "evidence_kind": "asserted", "valid_from_ms": 10, "source_revision_id": "industry-rev"}],
        as_of_ms=800,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    sizing = store.market_sizing(
        NS,
        artifact_id="sizing:compose",
        version=1,
        model_type="bottom_up",
        segments=[{"segment_id": "us-enterprise", "geography": "US", "customer_segment": "enterprise", "unit": "customer-year", "period": "2026", "customers": 100, "annual_price": 10, "adoption": 0.5, "capture": 0.2, "value_kind": "assumption", "source_revision_id": "sizing-rev"}],
        scenarios={"base": {"multiplier": 1}, "downside": {"multiplier": 0.8}},
        sensitivity=[],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    thesis = store.save_thesis(
        NS,
        thesis_id="thesis:compose",
        version=1,
        question="Will demand grow?",
        thesis="Demand will grow",
        alternatives=["Pricing may constrain adoption"],
        catalysts=["Product launch"],
        horizon="2026",
        assumptions=["No recession"],
        evidence=[{"stance": "contradicting", "source_revision_ids": ["thesis-evidence-rev"]}],
        falsification_conditions=[{"condition": "retention falls"}],
        watch_ids=[],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )

    composed = MarketCapabilityService(conn).invoke(
        "generate_market_brief",
        {
            "namespace": NS,
            "report_id": "brief:composed",
            "version": 1,
            "title": "Cutoff-pinned research",
            "sections": [],
            "cutoff_ms": 900,
            "formula_versions": [],
            "assumptions": [],
            "source_locators": [],
            "artifact_refs": [
                {"artifact_id": dossier["artifact_id"], "version": 1},
                {"artifact_id": industry["artifact_id"], "version": 1},
                {"artifact_id": sizing["artifact_id"], "version": 1},
                {"artifact_id": thesis["artifact_id"], "version": 1},
            ],
            "compose_artifacts": True,
            "owner": "alice",
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    assert composed["ok"]
    brief = composed["result"]

    assert brief["generation_mode"] == "artifact_composition"
    assert len(brief["sections"]) == 4
    assert {"noesis-market-dossier-delta-v1", "noesis-market-sizing-v1"} <= set(brief["formula_versions"])
    assert any("No recession" in item for item in brief["assumptions"])
    assert any(chart["chart_id"] == "sizing:compose:sizing-totals" for chart in brief["charts"])
    assert all(item.get("record_hash") for item in brief["artifact_refs"])
    revision_ids = set(brief["input_manifest"]["source_revision_ids"])
    assert {"release:revision-1", "public-evidence-rev", "comparison-rev", "industry-rev", "sizing-rev", "thesis-evidence-rev"} <= revision_ids
    assert "private-transcript-rev" not in revision_ids
    release_refs = [item for item in brief["source_locators"] if item.get("source_revision_id") == "release:revision-1"]
    assert len(release_refs) == 1 and release_refs[0]["entitlement_id"] == "fixture-entitlement"
    assert "restricted-transcript" not in brief["formats"]["markdown"]
    Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-brief-v1.json").read_text())).validate(brief)

    with pytest.raises(MarketResearchError, match="newer than the brief cutoff"):
        store.generate_brief(
            NS,
            report_id="brief:too-early",
            version=1,
            title="Too early",
            sections=[],
            cutoff_ms=799,
            formula_versions=[],
            assumptions=[],
            source_locators=[],
            artifact_refs=[{"artifact_id": industry["artifact_id"], "version": 1}],
            compose_artifacts=True,
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    conn.close()


def test_market_brief_export_example_matches_integrity_contract():
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-market-brief-export-v1.json").read_text()
    )
    example = json.loads(
        (ROOT / "contracts/examples/noesis-market-brief-export-v1.json").read_text()
    )

    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(example)
    assert verify_market_brief_export(example)["valid"] is True


def test_market_brief_evidence_bundle_rechecks_current_rights_without_leaking_payload():
    conn = duckdb.connect(":memory:")
    clock = {"now": 1000}
    entitlements = MarketEntitlementStore(conn, now=lambda: clock["now"])
    capabilities = [
        "ingest",
        "read",
        "display",
        "retain",
        "derive",
        "cache",
        "evidence",
        "export",
        "redistribute",
    ]
    entitlements.put_entitlement(
        NS,
        "entitlement:brief-evidence",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=capabilities,
        evidence_ref="fixture:terms-review",
        decision_ref="fixture:decision",
        principal_id="rights-admin",
        scopes={"operator"},
        effective_at_ms=1000,
    )
    store = MarketResearchStore(conn, now=lambda: clock["now"])
    store.generate_brief(
        NS,
        report_id="brief:evidence-rights",
        version=1,
        title="Rights-aware evidence export",
        sections=[
            {
                "heading": "Finding",
                "body": "DERIVED_REPORT_TEXT_MUST_BE_WITHHELD_AFTER_REVOCATION",
            }
        ],
        cutoff_ms=900,
        formula_versions=["formula:fixture-v1"],
        assumptions=["fixture assumptions"],
        source_locators=[
            {
                "source_ref_id": "source-ref:fixture-1",
                "source_revision_id": "source-revision:fixture-1",
                "provider": "fixture-provider",
                "license_id": "fixture-license",
                "entitlement_id": "entitlement:brief-evidence",
                "locator": "https://fixture.invalid/report",
                "content_hash": "a" * 64,
                "retrieved_at_ms": 900,
                "excerpt": "SOURCE_PAYLOAD_MUST_NEVER_BE_EMBEDDED",
            }
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    scopes = SCOPES | {"operator"}
    arguments = {
        "namespace": NS,
        "report_id": "brief:evidence-rights",
        "version": 1,
        "external": False,
        "owner": "alice",
    }

    authorized = MarketCapabilityService(conn).invoke(
        "export_market_brief_evidence_bundle",
        arguments,
        principal_id="alice",
        scopes=scopes,
    )
    assert authorized["ok"] is True
    assert verify_bundle(authorized["result"]).status == "valid"
    authorized_json = json.dumps(authorized["result"])
    assert "DERIVED_REPORT_TEXT_MUST_BE_WITHHELD_AFTER_REVOCATION" in authorized_json
    assert "SOURCE_PAYLOAD_MUST_NEVER_BE_EMBEDDED" not in authorized_json

    external_arguments = {**arguments, "external": True}
    authorized_external = MarketCapabilityService(conn).invoke(
        "export_market_brief_evidence_bundle",
        external_arguments,
        principal_id="alice",
        scopes=scopes,
    )
    assert authorized_external["ok"] is True
    assert verify_bundle(authorized_external["result"]).status == "valid"

    limited_capabilities = [
        capability for capability in capabilities if capability != "redistribute"
    ]
    entitlements.put_entitlement(
        NS,
        "entitlement:brief-evidence",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=limited_capabilities,
        evidence_ref="fixture:terms-review",
        decision_ref="fixture:no-redistribution",
        principal_id="rights-admin",
        scopes={"operator"},
        effective_at_ms=1000,
        expected_revision=1,
    )
    restricted_external = MarketCapabilityService(conn).invoke(
        "export_market_brief_evidence_bundle",
        external_arguments,
        principal_id="alice",
        scopes=scopes,
    )
    assert restricted_external["ok"] is True
    assert verify_bundle(restricted_external["result"]).status == "incomplete"
    assert "DERIVED_REPORT_TEXT_MUST_BE_WITHHELD_AFTER_REVOCATION" not in json.dumps(
        restricted_external["result"]
    )

    clock["now"] = 1001
    entitlements.put_entitlement(
        NS,
        "entitlement:brief-evidence",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=capabilities,
        evidence_ref="fixture:terms-review",
        decision_ref="fixture:revocation",
        principal_id="rights-admin",
        scopes={"operator"},
        status="revoked",
        effective_at_ms=1001,
        expected_revision=2,
    )
    revoked = MarketCapabilityService(conn).invoke(
        "export_market_brief_evidence_bundle",
        arguments,
        principal_id="alice",
        scopes=scopes,
    )
    assert revoked["ok"] is True
    assert verify_bundle(revoked["result"]).status == "incomplete"
    revoked_json = json.dumps(revoked["result"])
    assert "DERIVED_REPORT_TEXT_MUST_BE_WITHHELD_AFTER_REVOCATION" not in revoked_json
    assert "https://fixture.invalid/report" not in revoked_json
    assert "SOURCE_PAYLOAD_MUST_NEVER_BE_EMBEDDED" not in revoked_json
    receipt = next(
        item["payload"]
        for item in revoked["result"]["objects"]
        if item["type"] == "receipt"
    )
    assert receipt["status"] == "withheld"
    assert receipt["rights"]["reason_code"] == "entitlement_revoked"
    conn.close()
