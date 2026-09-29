"""Fixture-only checks for stable market instrument identity and replay."""

from __future__ import annotations

import duckdb
import hashlib
import pytest

from src.domains.market.instruments import MarketInstrumentError, MarketInstrumentStore
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

T0 = 1_700_000_000_000
T1 = T0 + 10_000
T2 = T0 + 20_000
NAMESPACE = "market:test"
PRINCIPAL = "analyst:test"
SCOPES = {"operator"}


@pytest.fixture
def setup_master():
    clock = {"now": T0}
    conn = duckdb.connect(":memory:")
    register_market_entitlement(
        conn, NAMESPACE, "entitlement:fixture", "fixture-only", "fixture-only", now_ms=T0
    )
    store = MarketInstrumentStore(conn, now=lambda: clock["now"])
    return store, clock, conn


def source_ref(
    name: str,
    retrieved_at_ms: int = T0,
    *,
    public_at_ms: int | None = None,
) -> dict:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return {
        "source_ref_id": f"src:{name}",
        "provider": "fixture-only",
        "provider_object_id": name,
        "source_revision_id": f"fixture:{name}:1",
        "public_at_ms": retrieved_at_ms if public_at_ms is None else public_at_ms,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": None,
        "retrieved_at_ms": retrieved_at_ms,
        "content_hash": digest,
        "license_id": "fixture-only",
        "entitlement_id": "entitlement:fixture",
    }


def add_issuer(store, issuer_id: str, clock, *, cik: str | None = None):
    ref = source_ref(f"{issuer_id}:{clock['now']}", clock["now"])
    identifiers = []
    if cik:
        identifiers.append(
            {
                "scheme": "cik",
                "value": cik,
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": ref["source_ref_id"],
            }
        )
    return store.put_issuer(
        NAMESPACE,
        issuer_id=issuer_id,
        kg_entity_id=f"kg:{issuer_id}",
        display_name=f"Issuer {issuer_id}",
        identifiers=identifiers,
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )


def add_security(
    store, issuer_id: str, security_id: str, clock, *, share_class="Common"
):
    ref = source_ref(f"{security_id}:{clock['now']}", clock["now"])
    return store.put_security(
        NAMESPACE,
        security_id=security_id,
        issuer_id=issuer_id,
        security_type="common_equity",
        share_class=share_class,
        denomination_currency="USD",
        identifiers=[],
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )


def add_listing(
    store,
    security_id: str,
    listing_id: str,
    ticker: str,
    clock,
    *,
    mic="XNAS",
    valid_from_ms=T0,
    valid_to_ms=None,
    status="active",
):
    ref = source_ref(f"{listing_id}:{clock['now']}", clock["now"])
    return store.put_listing(
        NAMESPACE,
        listing_id=listing_id,
        security_id=security_id,
        mic=mic,
        currency="USD",
        timezone="America/New_York",
        valid_from_ms=valid_from_ms,
        valid_to_ms=valid_to_ms,
        status=status,
        ticker_assertions=[
            {
                "value": ticker,
                "valid_from_ms": valid_from_ms,
                "valid_to_ms": valid_to_ms,
                "source_ref_id": ref["source_ref_id"],
            }
        ],
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )


def resolve_symbol(
    store, value, effective_ms, acquired_ms, *, mic=None, public_ms=None
):
    return store.resolve_symbol(
        NAMESPACE,
        value,
        as_of_ms=effective_ms,
        acquired_by_ms=acquired_ms,
        publicly_available_by_ms=acquired_ms if public_ms is None else public_ms,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        mic=mic,
    )


def test_ticker_reuse_resolves_by_valid_time_and_never_rewrites_old_listing(
    setup_master,
):
    store, clock, conn = setup_master
    add_issuer(store, "issuer:first", clock)
    add_security(store, "issuer:first", "security:first", clock)
    old_listing = add_listing(
        store,
        "security:first",
        "listing:first",
        "REUSE",
        clock,
        valid_from_ms=T0,
        valid_to_ms=T1,
    )

    clock["now"] = T2
    add_issuer(store, "issuer:second", clock)
    add_security(store, "issuer:second", "security:second", clock)
    add_listing(
        store,
        "security:second",
        "listing:second",
        "REUSE",
        clock,
        valid_from_ms=T1,
    )

    before_reuse = resolve_symbol(store, "reuse", T1 - 1, T2)
    after_reuse = resolve_symbol(store, "REUSE", T1, T2)

    assert before_reuse["status"] == "resolved"
    assert before_reuse["candidates"][0]["listing"]["listing_id"] == "listing:first"
    assert after_reuse["status"] == "resolved"
    assert after_reuse["candidates"][0]["listing"]["listing_id"] == "listing:second"
    assert (
        store.get_instrument(
            NAMESPACE,
            "listing",
            "listing:first",
            acquired_by_ms=T2,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )["revision_id"]
        == old_listing["revision_id"]
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM market_instrument_object_revisions WHERE object_type='listing'"
        ).fetchone()[0]
        == 2
    )


def test_ambiguous_cross_listing_is_queued_and_exchange_filter_resolves(setup_master):
    store, clock, _ = setup_master
    add_issuer(store, "issuer:cross", clock)
    add_security(store, "issuer:cross", "security:cross", clock)
    add_listing(store, "security:cross", "listing:nasdaq", "CROSS", clock, mic="XNAS")
    add_listing(store, "security:cross", "listing:nyse", "CROSS", clock, mic="XNYS")

    ambiguous = resolve_symbol(store, "CROSS", T0 + 1, T0 + 2)
    narrowed = resolve_symbol(store, "CROSS", T0 + 1, T0 + 2, mic="XNYS")

    assert ambiguous["status"] == "ambiguous"
    assert len(ambiguous["candidates"]) == 2
    assert ambiguous["review_id"]
    assert narrowed["status"] == "resolved"
    assert narrowed["candidates"][0]["listing"]["mic"] == "XNYS"
    reviews = store.list_mapping_reviews(
        NAMESPACE, principal_id=PRINCIPAL, scopes=SCOPES
    )
    assert len(reviews) == 1
    assert reviews[0]["reason"] == "multiple_active_mappings"


def test_public_cutoff_excludes_aliases_not_yet_public(setup_master):
    store, clock, _ = setup_master
    add_issuer(store, "issuer:late", clock)
    add_security(store, "issuer:late", "security:late", clock)
    clock["now"] = T1
    ref = source_ref("listing-public-late", T1, public_at_ms=T0 + 500)
    store.put_listing(
        NAMESPACE,
        listing_id="listing:late",
        security_id="security:late",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        valid_from_ms=T0,
        valid_to_ms=None,
        status="active",
        ticker_assertions=[
            {
                "value": "LATE",
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": ref["source_ref_id"],
            }
        ],
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    before_public = resolve_symbol(store, "LATE", T0 + 1, T1 + 1, public_ms=T0 + 499)
    after_public = resolve_symbol(store, "LATE", T0 + 1, T1 + 1, public_ms=T0 + 500)

    assert before_public["status"] == "unresolved"
    assert before_public["query"]["historical_public_cutoff_applied"] is True
    assert after_public["status"] == "resolved"


def test_identifier_correction_keeps_prior_revision_and_alias_provenance(setup_master):
    store, clock, _ = setup_master
    first_ref = source_ref("issuer-cik-first", T0)
    first = store.put_issuer(
        NAMESPACE,
        issuer_id="issuer:cik",
        kg_entity_id="kg:issuer:cik",
        display_name="CIK Example",
        source_refs=[first_ref],
        identifiers=[
            {
                "scheme": "cik",
                "value": "1234",
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": first_ref["source_ref_id"],
            }
        ],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    clock["now"] = T1
    corrected_ref = source_ref("issuer-cik-corrected", T1)
    second = store.put_issuer(
        NAMESPACE,
        issuer_id="issuer:cik",
        kg_entity_id="kg:issuer:cik",
        display_name="CIK Example",
        source_refs=[corrected_ref],
        identifiers=[
            {
                "scheme": "cik",
                "value": "1234",
                "valid_from_ms": T0,
                "valid_to_ms": T0,
                "assertion_status": "retracted",
                "source_ref_id": corrected_ref["source_ref_id"],
            },
            {
                "scheme": "cik",
                "value": "5678",
                "valid_from_ms": T1,
                "valid_to_ms": None,
                "source_ref_id": corrected_ref["source_ref_id"],
            },
        ],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        expected_revision=1,
    )

    old_vintage = store.resolve_identifier(
        NAMESPACE,
        "CIK",
        "1234",
        object_type="issuer",
        as_of_ms=T0 + 1,
        acquired_by_ms=T0 + 2,
        publicly_available_by_ms=T0 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    old_alias_after_correction = store.resolve_identifier(
        NAMESPACE,
        "cik",
        "1234",
        object_type="issuer",
        as_of_ms=T1 + 1,
        acquired_by_ms=T1 + 2,
        publicly_available_by_ms=T1 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    new_alias = store.resolve_identifier(
        NAMESPACE,
        "cik",
        "5678",
        object_type="issuer",
        as_of_ms=T1 + 1,
        acquired_by_ms=T1 + 2,
        publicly_available_by_ms=T1 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    as_known_before_correction = store.get_instrument(
        NAMESPACE,
        "issuer",
        "issuer:cik",
        acquired_by_ms=T0 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert first["revision"] == 1 and second["revision"] == 2
    assert old_vintage["status"] == "resolved"
    assert old_alias_after_correction["status"] == "unresolved"
    assert new_alias["status"] == "resolved"
    assert as_known_before_correction["revision_id"] == first["revision_id"]


def test_licensed_security_identifier_resolves_with_source_provenance(setup_master):
    store, clock, _ = setup_master
    add_issuer(store, "issuer:figi", clock)
    ref = source_ref("licensed-figi", T0)
    store.put_security(
        NAMESPACE,
        security_id="security:figi",
        issuer_id="issuer:figi",
        security_type="common_equity",
        share_class="A",
        denomination_currency="USD",
        identifiers=[
            {
                "scheme": "figi",
                "value": "BBG0000000001",
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": ref["source_ref_id"],
            }
        ],
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    result = store.resolve_identifier(
        NAMESPACE,
        "FIGI",
        "BBG0000000001",
        object_type="security",
        as_of_ms=T0 + 1,
        acquired_by_ms=T0 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert result["status"] == "resolved"
    assert result["candidates"][0]["security"]["security_id"] == "security:figi"
    assert (
        result["candidates"][0]["matched_alias"]["source_ref_id"]
        == ref["source_ref_id"]
    )


def test_unresolved_symbol_can_be_dismissed_only_by_owner_or_operator(setup_master):
    store, _, _ = setup_master
    result = resolve_symbol(store, "MISSING", T0, T0 + 1)
    assert result["status"] == "unresolved"
    assert result["reason"] == "no_active_mapping"
    review = store.list_mapping_reviews(
        NAMESPACE, principal_id=PRINCIPAL, scopes=SCOPES
    )[0]
    decision = store.decide_mapping_review(
        NAMESPACE,
        review["review_id"],
        decision="dismissed",
        note="Confirmed that this symbol is not part of the pilot universe.",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert decision["decision"] == "dismissed"
    assert (
        store.list_mapping_reviews(NAMESPACE, principal_id=PRINCIPAL, scopes=SCOPES)
        == []
    )


def test_share_classes_cross_listings_merger_and_universe_history_are_retained(
    setup_master,
):
    store, clock, conn = setup_master
    add_issuer(store, "issuer:old", clock)
    add_issuer(store, "issuer:new", clock)
    add_security(store, "issuer:old", "security:common", clock, share_class="Common")
    add_security(
        store, "issuer:old", "security:preferred", clock, share_class="Preferred"
    )
    add_listing(
        store, "security:common", "listing:common-xnas", "EXM", clock, mic="XNAS"
    )
    add_listing(
        store, "security:common", "listing:common-otc", "EXM", clock, mic="OTCQ"
    )
    add_listing(
        store,
        "security:preferred",
        "listing:preferred-xnas",
        "EXM.P",
        clock,
        mic="XNAS",
    )
    relationship_ref = source_ref("merger-announcement", T0)
    relationship = store.put_issuer_relationship(
        NAMESPACE,
        issuer_id="issuer:old",
        related_issuer_id="issuer:new",
        relationship_type="merged_into",
        effective_date="2023-11-16",
        source_refs=[relationship_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        issuer_relationship_id="relationship:old-into-new",
    )
    membership_ref = source_ref("universe-inclusion", T0)
    included = store.put_universe_membership(
        NAMESPACE,
        universe_id="universe:pilot",
        security_id="security:common",
        membership_status="included",
        valid_from_ms=T0,
        valid_to_ms=None,
        source_refs=[membership_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    clock["now"] = T1
    excluded_ref = source_ref("universe-correction", T1)
    store.put_universe_membership(
        NAMESPACE,
        universe_id="universe:pilot",
        security_id="security:common",
        membership_status="excluded",
        valid_from_ms=T0,
        valid_to_ms=None,
        source_refs=[excluded_ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        expected_revision=1,
    )

    before_correction = store.list_universe_members(
        NAMESPACE,
        "universe:pilot",
        as_of_ms=T0 + 1,
        acquired_by_ms=T0 + 2,
        publicly_available_by_ms=T0 + 2,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    after_correction = store.list_universe_members(
        NAMESPACE,
        "universe:pilot",
        as_of_ms=T0 + 1,
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T1 + 1,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    relations = store.list_issuer_relationships(
        NAMESPACE,
        "issuer:old",
        acquired_by_ms=T1,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert included["revision"] == 1
    assert len(before_correction) == 1
    assert after_correction == []
    assert (
        relations[0]["issuer_relationship_id"] == relationship["issuer_relationship_id"]
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM market_instrument_object_revisions WHERE object_type='security'"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM market_instrument_object_revisions WHERE object_type='listing'"
        ).fetchone()[0]
        == 3
    )


def test_listing_cannot_be_delisted_without_an_effective_boundary(setup_master):
    store, clock, _ = setup_master
    add_issuer(store, "issuer:delist", clock)
    add_security(store, "issuer:delist", "security:delist", clock)
    with pytest.raises(MarketInstrumentError, match="delisting boundary"):
        add_listing(
            store,
            "security:delist",
            "listing:delist",
            "DEAD",
            clock,
            status="inactive",
            valid_to_ms=None,
        )


def test_delisted_listing_remains_resolvable_only_before_its_end_date(setup_master):
    store, clock, _ = setup_master
    add_issuer(store, "issuer:delisted", clock)
    add_security(store, "issuer:delisted", "security:delisted", clock)
    add_listing(store, "security:delisted", "listing:delisted", "GONE", clock)

    clock["now"] = T1
    ref = source_ref("delisting-notice", T1)
    store.put_listing(
        NAMESPACE,
        listing_id="listing:delisted",
        security_id="security:delisted",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        valid_from_ms=T0,
        valid_to_ms=T1,
        status="inactive",
        ticker_assertions=[
            {
                "value": "GONE",
                "valid_from_ms": T0,
                "valid_to_ms": T1,
                "source_ref_id": ref["source_ref_id"],
            }
        ],
        source_refs=[ref],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        expected_revision=1,
    )

    historical = resolve_symbol(store, "GONE", T1 - 1, T1 + 1)
    delisted = resolve_symbol(store, "GONE", T1 + 1, T1 + 1)
    retained = store.get_instrument(
        NAMESPACE,
        "listing",
        "listing:delisted",
        acquired_by_ms=T1 + 1,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert historical["status"] == "resolved"
    assert delisted["status"] == "unresolved"
    assert retained["status"] == "inactive"
    assert retained["revision"] == 2
