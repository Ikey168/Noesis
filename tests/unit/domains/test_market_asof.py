"""Point-in-time manifests compose existing market and document revisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.asof import MarketAsOfError, MarketAsOfSnapshotStore
from src.domains.market.actions import MarketCorporateActionStore
from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.prices import MarketPriceStore
from src.domains.economic.releases import EconomicReleaseStore
from src.domains.economic.model import load_fixture
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.quantitative import QuantitativeStore
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = "market:asof-test"
PRINCIPAL = "analyst:asof-test"
SCOPES = {"operator"}
T0 = 1_790_000_000_000
T1 = T0 + 10_000


def source_ref(name: str, acquired_at: int, public_at: int | None = None) -> dict:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return {
        "source_ref_id": f"src:{name}",
        "provider": "fixture-only",
        "provider_object_id": name,
        "source_revision_id": f"fixture:{name}:1",
        "public_at_ms": acquired_at if public_at is None else public_at,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": None,
        "retrieved_at_ms": acquired_at,
        "content_hash": digest,
        "license_id": "fixture-only",
        "entitlement_id": "entitlement:fixture",
    }


def seeded_market(*, inactive_listing: bool = False):
    clock = {"now": T0}
    conn = duckdb.connect(":memory:")
    register_market_entitlement(
        conn, NAMESPACE, "entitlement:fixture", "fixture-only", "fixture-only", now_ms=T0
    )
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    prices = MarketPriceStore(conn, now=lambda: clock["now"])
    issuer_ref = source_ref("issuer", T0)
    instruments.put_issuer(
        NAMESPACE,
        issuer_id="issuer:asof",
        kg_entity_id="kg:issuer:asof",
        display_name="As Of Fixture Inc.",
        source_refs=[issuer_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    security_ref = source_ref("security", T0)
    instruments.put_security(
        NAMESPACE,
        security_id="security:asof",
        issuer_id="issuer:asof",
        security_type="common_equity",
        share_class="A",
        denomination_currency="USD",
        source_refs=[security_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    listing_ref = source_ref("listing", T0)
    listing_id = "listing:asof-inactive" if inactive_listing else "listing:asof"
    listing = instruments.put_listing(
        NAMESPACE,
        listing_id=listing_id,
        security_id="security:asof",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        status="inactive" if inactive_listing else "active",
        valid_from_ms=T0,
        valid_to_ms=T0 + 500 if inactive_listing else None,
        ticker_assertions=[
            {
                "value": "ASOF",
                "valid_from_ms": T0,
                "valid_to_ms": T0 + 500 if inactive_listing else None,
                "source_ref_id": listing_ref["source_ref_id"],
            }
        ],
        source_refs=[listing_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    return conn, clock, instruments, prices, listing


def bar(start_ms: int, *, close: float, retrieved_at_ms: int, public_at_ms: int) -> dict:
    ref = source_ref(
        f"bar-{retrieved_at_ms}", retrieved_at_ms, public_at=public_at_ms
    )
    return {
        "contract": "noesis-market-bar-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "bar_id": "provider-id-is-replaced",
        "listing_id": "listing:asof",
        "provider": "fixture-provider",
        "interval": "1d",
        "bar_start_ms": start_ms,
        "bar_end_ms": start_ms + 1_000,
        "public_at_ms": public_at_ms,
        "retrieved_at_ms": retrieved_at_ms,
        "revision_id": "provider-revision-is-replaced",
        "revision": 1,
        "provider_record_id": f"bar:{start_ms}",
        "provider_revision_id": f"fixture:{retrieved_at_ms}",
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 100,
        "trade_count": None,
        "vwap": close,
        "currency": "USD",
        "price_basis": "unadjusted",
        "adjustment_method": None,
        "adjustment_cutoff_ms": None,
        "adjustment_action_revision_ids": [],
        "adjustment_calculation_id": None,
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": retrieved_at_ms,
        "record_hash": "ignored",
    }


def snapshot_request(*, key: str, public_cutoff: int, acquired_cutoff: int, selection):
    return {
        "namespace": NAMESPACE,
        "request_key": key,
        "effective_at_ms": T1 + 1_000,
        "publicly_available_by_ms": public_cutoff,
        "acquired_by_ms": acquired_cutoff,
        "selection": selection,
        "principal_id": PRINCIPAL,
        "scopes": SCOPES,
    }


def test_corrected_bars_are_pinned_to_each_pair_of_cutoffs_and_replay_immutably():
    conn, clock, _, prices, _ = seeded_market()
    start = T0 + 100
    original = prices.put_bar(
        NAMESPACE,
        bar(start, close=101.0, retrieved_at_ms=T0, public_at_ms=T0),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    clock["now"] = T1
    correction = prices.put_bar(
        NAMESPACE,
        bar(start, close=103.0, retrieved_at_ms=T1, public_at_ms=T1),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        expected_revision=1,
    )
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    selection = {
        "prices": [
            {
                "listing_id": "listing:asof",
                "interval": "1d",
                "start_ms": T0,
                "end_ms": T0 + 5_000,
            }
        ]
    }
    old = store.create_snapshot(
        **snapshot_request(
            key="close-before-correction",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection=selection,
        )
    )
    new = store.create_snapshot(
        **snapshot_request(
            key="close-after-correction",
            public_cutoff=T1,
            acquired_cutoff=T1,
            selection=selection,
        )
    )

    old_bar = next(item for item in old["inputs"] if item["kind"] == "price_bar")
    new_bar = next(item for item in new["inputs"] if item["kind"] == "price_bar")
    assert old_bar["revision_id"] == original["revision_id"]
    assert new_bar["revision_id"] == correction["revision_id"]
    assert old["coverage"]["status"] == "partial"
    assert old["coverage"]["unproven_price_ranges"]
    assert store.inspect_snapshot(
        NAMESPACE, old["snapshot_id"], principal_id=PRINCIPAL, scopes=SCOPES
    )["input_hash"] == old["input_hash"]
    with pytest.raises(MarketAsOfError) as incomplete:
        store.create_snapshot(
            **snapshot_request(
                key="strict-price-coverage",
                public_cutoff=T0,
                acquired_cutoff=T0,
                selection=selection,
            ),
            gap_policy="fail",
        )
    assert incomplete.value.code == "historical_inputs_incomplete"
    assert incomplete.value.details["coverage"]["unproven_price_ranges"]
    assert store.create_snapshot(
        **snapshot_request(
            key="close-before-correction",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection=selection,
        )
    )["idempotent"] is True

    schema_path = ROOT / "contracts/schemas/jsonschema/noesis-market-asof-snapshot-v1.json"
    Draft7Validator(json.loads(schema_path.read_text())).validate(old)
    conn.close()


def test_late_documents_are_gaps_and_fail_policy_rejects_them():
    conn, _, _, _, _ = seeded_market()
    revisions = DocumentRevisionStore(conn)
    revisions.observe(
        {
            "document_id": "doc:late",
            "source_type": "filing",
            "language": "en",
            "ingested_at": T1,
            "created_at": T0,
            "title": "Late filing",
            "content": "fixture text",
            "metadata": {"public_at_ms": T0},
        }
    )
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    selection = {"documents": [{"document_id": "doc:late"}]}
    recorded = store.create_snapshot(
        **snapshot_request(
            key="late-document-recorded",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection=selection,
        )
    )
    assert recorded["gaps"] == [
        {"kind": "document", "object_id": "doc:late", "reason": "not_acquired_by_cutoff"}
    ]
    with pytest.raises(MarketAsOfError, match="incomplete") as exc:
        store.create_snapshot(
            **snapshot_request(
                key="late-document-fails",
                public_cutoff=T0,
                acquired_cutoff=T0,
                selection=selection,
            ),
            gap_policy="fail",
        )
    assert exc.value.code == "historical_inputs_incomplete"
    conn.close()


def test_amended_filing_uses_revision_available_at_requested_cutoff():
    conn = duckdb.connect(":memory:")
    revisions = DocumentRevisionStore(conn)
    first = {
        "document_id": "filing:amended",
        "source_type": "filing",
        "language": "en",
        "ingested_at": T0,
        "created_at": T0,
        "title": "Quarterly filing",
        "content": "Original filed value was 100.",
        "metadata": {"public_at_ms": T0},
    }
    original = revisions.observe(first)
    amended = {
        **first,
        "ingested_at": T1,
        "content": "Amended filed value was 110.",
        "metadata": {"public_at_ms": T1},
    }
    correction = revisions.observe(amended)
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    selection = {"documents": [{"document_id": "filing:amended"}]}
    earlier = store.create_snapshot(
        **snapshot_request(
            key="before-filing-amendment",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection=selection,
        )
    )
    later = store.create_snapshot(
        **snapshot_request(
            key="after-filing-amendment",
            public_cutoff=T1,
            acquired_cutoff=T1,
            selection=selection,
        )
    )
    assert next(i for i in earlier["inputs"] if i["kind"] == "document")["revision_id"] == original[
        "revision_id"
    ]
    assert next(i for i in later["inputs"] if i["kind"] == "document")["revision_id"] == correction[
        "revision_id"
    ]
    conn.close()


def test_economic_release_snapshot_is_composed_without_guessing_public_time():
    conn = duckdb.connect(":memory:")
    fixture = json.loads((ROOT / "tests/fixtures/economic/benchmark.json").read_text())
    load_fixture(conn, fixture)
    economics = EconomicReleaseStore(conn, now=lambda: T1)
    release = economics.create_snapshot(
        "economics",
        "macro-asof-fixture",
        "fixture-quarter-release",
        release_cutoff_ms=1_756_684_800_000,
        acquired_cutoff_ms=1_756_684_800_000,
        series=[{"series_id": "fred:GDPC1:US"}],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    manifest = store.create_snapshot(
        **snapshot_request(
            key="macro-release-composition",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection={
                "economic_snapshots": [
                    {"namespace": "economics", "snapshot_id": release["snapshot_id"]}
                ]
            },
        )
    )
    item = next(i for i in manifest["inputs"] if i["kind"] == "economic_snapshot")
    assert item["public_at_ms"] is None
    assert item["release_cutoff_ms"] == 1_756_684_800_000
    assert item["series"][0]["observation_ids"]
    assert store.inspect_snapshot(
        NAMESPACE, manifest["snapshot_id"], principal_id=PRINCIPAL, scopes=SCOPES
    )["input_hash"] == manifest["input_hash"]
    conn.close()


def test_inactive_listing_is_marked_and_can_fail_closed():
    conn, _, _, _, listing = seeded_market(inactive_listing=True)
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    selection = {
        "listings": [listing["listing_id"]],
        "prices": [
            {
                "listing_id": listing["listing_id"],
                "interval": "1d",
                "start_ms": T0,
                "end_ms": T0 + 5_000,
            }
        ],
    }
    recorded = store.create_snapshot(
        **snapshot_request(
            key="inactive-listing-recorded",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection=selection,
        )
    )
    assert any(
        gap["kind"] == "listing" and gap["reason"] == "not_active_at_effective_time"
        for gap in recorded["gaps"]
    )
    assert any(
        gap["kind"] == "prices" and gap["reason"] == "no_history_retained_at_cutoffs"
        for gap in recorded["gaps"]
    )
    with pytest.raises(MarketAsOfError) as exc:
        store.create_snapshot(
            **snapshot_request(
                key="inactive-listing-fails",
                public_cutoff=T0,
                acquired_cutoff=T0,
                selection=selection,
            ),
            gap_policy="fail",
        )
    assert exc.value.code == "historical_inputs_incomplete"
    conn.close()


def test_quantitative_transform_is_pinned_to_selected_input_revisions():
    conn, _, _, prices, _ = seeded_market()
    observation = prices.put_bar(
        NAMESPACE,
        bar(T0 + 100, close=101.0, retrieved_at_ms=T0, public_at_ms=T0),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    calculation = QuantitativeStore(conn, now=lambda: T0 + 1).record_domain_calculation(
        NAMESPACE,
        "fixture-market-return",
        {"price_basis": "unadjusted"},
        {"return": "0.0"},
        input_ids=[observation["revision_id"]],
        principal_id=PRINCIPAL,
        scopes={"knowledge:quantitative:calculate"},
        formula_revision_id="formula:market-return:v1",
    )
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1 + 1)
    manifest = store.create_snapshot(
        **snapshot_request(
            key="pinned-market-return",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection={
                "prices": [
                    {
                        "listing_id": "listing:asof",
                        "interval": "1d",
                        "start_ms": T0,
                        "end_ms": T0 + 5_000,
                    }
                ]
            },
        ),
        transformations=[
            {"kind": "quantitative", "calculation_id": calculation["calculation_id"]}
        ],
    )
    transformation = manifest["transformations"][0]
    assert transformation["deterministic"] is True
    assert transformation["input_ids"] == [observation["revision_id"]]
    assert transformation["missing_input_ids"] == []
    assert transformation["formula_revision_id"] == "formula:market-return:v1"
    assert store.inspect_snapshot(
        NAMESPACE,
        manifest["snapshot_id"],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )["input_hash"] == manifest["input_hash"]
    conn.close()


def test_actions_and_as_filed_facts_are_composed_from_their_exact_revisions():
    conn, clock, _, _, _ = seeded_market()
    actions = MarketCorporateActionStore(conn, now=lambda: clock["now"])
    action_ref = source_ref("cash-dividend", T0)
    action = actions.put_action(
        NAMESPACE,
        {
            "contract": "noesis-market-corporate-action-v1",
            "namespace": NAMESPACE,
            "owner": None,
            "action_id": "action:asof-dividend",
            "issuer_id": "issuer:asof",
            "security_id": "security:asof",
            "listing_id": "listing:asof",
            "related_security_id": None,
            "related_listing_id": None,
            "revision_id": "provider-revision-is-replaced",
            "revision": 1,
            "provider": "fixture-provider",
            "provider_record_id": "provider:dividend:1",
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
            "cash_amount": "0.25",
            "cash_amount_basis": "per_share_before_action",
            "distribution_type": "regular",
            "currency": "USD",
            "prior_revision_id": None,
            "source_refs": [action_ref],
            "recorded_at_ms": T0,
            "record_hash": "ignored",
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    facts = MarketFinancialFactStore(conn, now=lambda: clock["now"])
    fact_ref = source_ref("filing:asof", T0)
    fact = facts.put_fact(
        NAMESPACE,
        {
            "contract": "noesis-market-financial-fact-v1",
            "namespace": NAMESPACE,
            "owner": None,
            "fact_observation_id": "fact:asof:revenue:fy2025",
            "issuer_id": "issuer:asof",
            "revision_id": "provider-fact-revision-is-replaced",
            "revision": 1,
            "filing_accession": "fixture-accession-2025",
            "filing_form": "10-K",
            "taxonomy": "us-gaap",
            "concept": "Revenues",
            "canonical_concept": "revenue",
            "statement": "income_statement",
            "mapping_status": "mapped",
            "context_id": "context:fy2025",
            "context_id_kind": "companyfacts_composite_key",
            "unit": "USD",
            "period": {
                "kind": "duration",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
            },
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "period_class": "annual",
            "value_lexical": "1234567",
            "scale": 0,
            "decimals": -3,
            "filed_at_ms": T0,
            "accepted_at_ms": T0,
            "public_at_ms": T0,
            "retrieved_at_ms": T0,
            "source_document_revision_id": "document-revision:fixture-filing",
            "source_locator": "companyfacts/us-gaap/Revenues/fixture-accession-2025",
            "provider": "fixture-provider",
            "prior_revision_id": None,
            "source_refs": [fact_ref],
            "recorded_at_ms": T0,
            "record_hash": "ignored",
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    store = MarketAsOfSnapshotStore(conn, now=lambda: T1)
    manifest = store.create_snapshot(
        **snapshot_request(
            key="actions-facts-asof",
            public_cutoff=T0,
            acquired_cutoff=T0,
            selection={
                "actions": [{"security_id": "security:asof"}],
                "financial_facts": [
                    {"issuer_id": "issuer:asof", "taxonomy": "us-gaap"}
                ],
            },
        )
    )
    selected_action = next(i for i in manifest["inputs"] if i["kind"] == "corporate_action")
    selected_fact = next(i for i in manifest["inputs"] if i["kind"] == "financial_fact")
    assert selected_action["revision_id"] == action["revision_id"]
    assert selected_fact["revision_id"] == fact["revision_id"]
    assert selected_fact["filing_accession"] == "fixture-accession-2025"
    assert manifest["gaps"] == []
    conn.close()


def test_provenance_export_requires_current_export_and_redistribution_rights():
    conn, clock, _, prices, _ = seeded_market()
    prices.put_bar(
        NAMESPACE,
        bar(T0 + 100, close=100.0, retrieved_at_ms=T0, public_at_ms=T0),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    entitlements = MarketEntitlementStore(conn, now=lambda: clock["now"])
    entitlements.put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=["read", "retain", "export"],
        evidence_ref="synthetic-fixture:rights-review:v1",
        decision_ref="synthetic-fixture:export-review:v1",
        principal_id="operator:rights-reviewer",
        scopes=SCOPES,
        effective_at_ms=0,
        expected_revision=1,
    )
    selection = {
        "listings": ["listing:asof"],
        "prices": [
            {
                "listing_id": "listing:asof",
                "interval": "1d",
                "start_ms": T0,
                "end_ms": T0 + 5_000,
            }
        ],
        "actions": [],
        "financial_facts": [],
        "economic_snapshots": [],
        "documents": [],
    }
    store = MarketAsOfSnapshotStore(conn, now=lambda: clock["now"])
    snapshot = store.create_snapshot(
        **snapshot_request(
            key="export-rights-snapshot",
            public_cutoff=T0 + 1_000,
            acquired_cutoff=T0 + 1_000,
            selection=selection,
        ),
        gap_policy="record",
    )
    export = store.export_snapshot(
        NAMESPACE,
        snapshot["snapshot_id"],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        external=False,
    )
    Draft7Validator(
        json.loads(
            (ROOT / "contracts/schemas/jsonschema/noesis-market-asof-export-v1.json").read_text()
        )
    ).validate(export)
    assert export["source_payloads_included"] is False
    assert export["rights_decisions"]
    assert all(
        "close" not in item and "series" not in item
        for item in export["snapshot"]["inputs"]
    )
    with pytest.raises(MarketAsOfError) as restricted:
        store.export_snapshot(
            NAMESPACE,
            snapshot["snapshot_id"],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
            external=True,
        )
    assert restricted.value.code == "operation_restricted"
    assert "redistribute" in restricted.value.details["missing_capabilities"]
    conn.close()
