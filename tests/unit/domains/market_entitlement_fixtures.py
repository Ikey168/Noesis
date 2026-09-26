"""Explicit synthetic provider rights shared by market domain fixtures."""

from src.domains.market.entitlements import MarketEntitlementStore

FIXTURE_CAPABILITIES = [
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


def register_market_entitlement(
    conn,
    namespace,
    entitlement_id,
    provider,
    license_id,
    *,
    now_ms=0,
):
    store = MarketEntitlementStore(conn, now=lambda: now_ms)
    return store.put_entitlement(
        namespace,
        entitlement_id,
        provider=provider,
        license_id=license_id,
        capabilities=FIXTURE_CAPABILITIES,
        evidence_ref="synthetic-fixture:rights-review:v1",
        decision_ref="synthetic-fixture:reviewer-decision:v1",
        principal_id="operator:fixture-reviewer",
        scopes={"operator"},
        effective_at_ms=0,
    )
