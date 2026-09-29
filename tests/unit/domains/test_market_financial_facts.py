"""Point-in-time revision and entitlement checks for filed financial facts."""

from __future__ import annotations

import hashlib

import duckdb
import pytest

from src.domains.market.financial_facts import (
    FACT_READ_SCOPE,
    FACT_WRITE_SCOPE,
    MarketFinancialFactError,
    MarketFinancialFactStore,
)
from src.domains.market.instruments import MarketInstrumentStore
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

NAMESPACE = "market:financial-facts-test"
PRINCIPAL = "analyst:financial-facts-test"
OPERATOR = {"operator"}
T0 = 1_750_000_000_000
T1 = T0 + 86_400_000


def source_ref(name: str, public_at_ms: int) -> dict:
    return {
        "source_ref_id": f"src:{name}",
        "provider": "sec-edgar",
        "provider_object_id": name,
        "source_revision_id": name,
        "public_at_ms": public_at_ms,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/{name}",
        "retrieved_at_ms": public_at_ms,
        "content_hash": hashlib.sha256(name.encode()).hexdigest(),
        "license_id": "sec-public",
        "entitlement_id": "sec-edgar-public",
    }


def fact_payload(
    accession: str,
    value: str,
    public_at_ms: int,
    *,
    concept="Revenues",
    context_id="context:fy2024",
    start_date="2024-01-01",
    end_date="2024-12-31",
    filing_form="10-K",
) -> dict:
    logical_id = f"fact:{accession}:{concept}:{context_id}:USD"
    ref = source_ref(accession, public_at_ms)
    return {
        "contract": "noesis-market-financial-fact-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "fact_observation_id": logical_id,
        "issuer_id": "issuer:financial-facts",
        "revision_id": f"{logical_id}@source",
        "revision": 1,
        "filing_accession": accession,
        "filing_form": filing_form,
        "taxonomy": "us-gaap",
        "concept": concept,
        "canonical_concept": "revenue" if concept == "Revenues" else None,
        "statement": "income_statement" if concept == "Revenues" else "other",
        "mapping_status": "mapped" if concept == "Revenues" else "unmapped",
        "context_id": context_id,
        "context_id_kind": "companyfacts_composite_key",
        "unit": "USD",
        "period": {
            "kind": "duration",
            "start_date": start_date,
            "end_date": end_date,
        },
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_class": "annual",
        "value_lexical": value,
        "scale": 0,
        "decimals": -3,
        "filed_at_ms": public_at_ms,
        "accepted_at_ms": public_at_ms,
        "public_at_ms": public_at_ms,
        "retrieved_at_ms": public_at_ms,
        "source_document_revision_id": f"sec-filing:{accession}@{accession}",
        "source_locator": f"companyfacts/us-gaap/{concept}/USD/{accession}/{context_id}",
        "provider": "sec-edgar",
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": public_at_ms,
        "record_hash": "0" * 64,
    }


@pytest.fixture
def fact_store():
    clock = {"now": T0}
    conn = duckdb.connect(":memory:")
    register_market_entitlement(
        conn,
        NAMESPACE,
        "sec-edgar-public",
        "sec-edgar",
        "sec-public",
        now_ms=T0,
    )
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    instruments.put_issuer(
        NAMESPACE,
        issuer_id="issuer:financial-facts",
        kg_entity_id="kg:issuer:financial-facts",
        display_name="Financial Facts Test Inc.",
        source_refs=[source_ref("issuer-master", T0)],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    store = MarketFinancialFactStore(conn, now=lambda: clock["now"])
    return conn, clock, store


def put(store, payload, *, expected_revision=None):
    return store.put_fact(
        NAMESPACE,
        payload,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
        expected_revision=expected_revision,
    )


def query(
    store,
    *,
    public_cutoff,
    acquired_cutoff,
    scopes=OPERATOR,
    **extra,
):
    return store.get_facts(
        NAMESPACE,
        "issuer:financial-facts",
        acquired_by_ms=acquired_cutoff,
        publicly_available_by_ms=public_cutoff,
        principal_id=PRINCIPAL,
        scopes=scopes,
        **extra,
    )


def test_amended_accession_is_retained_and_selected_only_after_publication(fact_store):
    conn, clock, store = fact_store
    original = put(store, fact_payload("accession-2024", "100", T0))
    clock["now"] = T1
    amended = put(store, fact_payload("accession-2024-amended", "110", T1))

    before = store.latest_facts_by_period(
        NAMESPACE,
        "issuer:financial-facts",
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T1 - 1,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    after = store.latest_facts_by_period(
        NAMESPACE,
        "issuer:financial-facts",
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T1 + 1,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    assert [
        fact["filing_accession"]
        for fact in query(store, public_cutoff=T1 - 1, acquired_cutoff=T1 + 1)
    ] == ["accession-2024"]
    assert before["facts"][0]["value_lexical"] == "100"
    assert after["facts"][0]["value_lexical"] == "110"
    assert amended["fact_observation_id"] != original["fact_observation_id"]
    assert (
        conn.execute("SELECT COUNT(*) FROM market_financial_fact_revisions").fetchone()[
            0
        ]
        == 2
    )


def test_same_accession_correction_is_append_only_and_cutoff_aware(fact_store):
    _, clock, store = fact_store
    original = put(store, fact_payload("accession-correction", "100", T0))
    clock["now"] = T1
    correction = fact_payload("accession-correction", "105", T1)
    revised = put(store, correction, expected_revision=1)

    before = query(store, public_cutoff=T1 - 1, acquired_cutoff=T1 + 1)
    after = query(store, public_cutoff=T1 + 1, acquired_cutoff=T1 + 1)

    assert original["revision"] == 1
    assert revised["revision"] == 2
    assert revised["prior_revision_id"] == original["revision_id"]
    assert before[0]["value_lexical"] == "100"
    assert after[0]["value_lexical"] == "105"


def test_identical_fact_replay_is_idempotent_and_stale_revision_conflicts(fact_store):
    _, _, store = fact_store
    payload = fact_payload("accession-replay", "100", T0)
    original = put(store, payload)
    replay = put(store, payload)
    changed = fact_payload("accession-replay", "101", T1)

    assert replay["revision_id"] == original["revision_id"]
    with pytest.raises(MarketFinancialFactError) as exc:
        put(store, changed, expected_revision=0)
    assert exc.value.code == "revision_conflict"


def test_latest_filed_query_keeps_same_accession_context_conflicts_visible(fact_store):
    _, clock, store = fact_store
    put(store, fact_payload("accession-context", "100", T0, context_id="context-a"))
    clock["now"] = T1
    put(
        store,
        fact_payload("accession-context", "101", T0, context_id="context-b"),
    )

    latest = store.latest_facts_by_period(
        NAMESPACE,
        "issuer:financial-facts",
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T1 + 1,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    assert len(latest["facts"]) == 2
    assert latest["readiness"] == "partial"
    assert latest["diagnostics"][0]["code"] == "conflicting_contexts"


def test_current_entitlement_blocks_fact_write_and_complete_read(fact_store):
    _, _, store = fact_store
    payload = fact_payload("accession-licensed", "100", T0)
    with pytest.raises(MarketFinancialFactError) as write_error:
        store.put_fact(
            NAMESPACE,
            payload,
            principal_id=PRINCIPAL,
            scopes={FACT_WRITE_SCOPE, f"namespace:{NAMESPACE}:write"},
        )
    assert write_error.value.code == "entitlement_unavailable"

    stored = put(store, payload)
    base_scopes = {
        FACT_READ_SCOPE,
        f"namespace:{NAMESPACE}:read",
    }
    assert (
        query(
            store,
            public_cutoff=T1,
            acquired_cutoff=T1,
            scopes=base_scopes,
        )
        == []
    )
    with pytest.raises(MarketFinancialFactError) as read_error:
        store.get_facts(
            NAMESPACE,
            "issuer:financial-facts",
            acquired_by_ms=T1,
            publicly_available_by_ms=T1,
            principal_id=PRINCIPAL,
            scopes=base_scopes,
            require_complete=True,
        )
    assert read_error.value.code == "fact_history_unavailable"

    entitled = store.get_facts(
        NAMESPACE,
        "issuer:financial-facts",
        acquired_by_ms=T1,
        publicly_available_by_ms=T1,
        principal_id=PRINCIPAL,
        scopes=base_scopes | {"market:entitlement:sec-edgar-public:read"},
    )
    assert entitled[0]["revision_id"] == stored["revision_id"]


def test_ingest_batch_returns_diagnostics_with_idempotent_count(fact_store):
    _, _, store = fact_store
    batch = {
        "facts": [fact_payload("accession-batch", "100", T0)],
        "diagnostics": [{"code": "unmapped_accounting_concept", "concept": "OtherTag"}],
        "readiness": "partial",
    }

    first = store.ingest_batch(
        NAMESPACE, batch, principal_id=PRINCIPAL, scopes=OPERATOR
    )
    second = store.ingest_batch(
        NAMESPACE, batch, principal_id=PRINCIPAL, scopes=OPERATOR
    )

    assert first["count"] == second["count"] == 1
    assert first["fact_revision_ids"] == second["fact_revision_ids"]
    assert second["diagnostics"][0]["code"] == "unmapped_accounting_concept"
