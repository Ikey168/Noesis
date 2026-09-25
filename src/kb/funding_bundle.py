"""The Funding & Grants bundle: declared contributions, enablement and readiness.

The pack/workflow composition runtime referenced by the planning issues
(``docs/architecture/pack-workflow-composition.md``, items C02–C07: resolved
bindings, lifecycle and authorized dispatch) is proposed, not shipped. This
module therefore *declares* the bundle's contributions as data and reports
actual readiness; it does not implement a composition runtime, scheduler,
permission ledger, project store or submission engine.

Disabling the bundle for a namespace blocks only the funding entry points.
Shared providers stay independently usable: ``DurableHTTP``, the document
store, research projects, authored reports, quantitative calculations and
subscriptions are never gated by this flag.
"""

from __future__ import annotations

import time

from src.ingestion.funding_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.funding_ranking import DEFAULT_WEIGHTS
from src.kb.funding_records import INSTRUMENT_KINDS, RECORD_KINDS, READ_SCOPE

BUNDLE_ID = "funding-grants"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "proposed; not present in this repository and no composition runtime is shipped",
        "depends_on": {key: "pending (unshipped composition runtime)" for key in ("C02", "C03", "C04", "C05", "C06", "C07")},
        "depends_on_semantics": "resolved bindings, lifecycle and authorized dispatch",
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "coverage": c["coverage"], "shared": True}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "ontology": {"contract": "noesis-funding-record-v1", "record_kinds": list(RECORD_KINDS),
                     "instrument_kinds": list(INSTRUMENT_KINDS)},
        "eligibility": {"contract": "noesis-funding-eligibility-v1", "semantics": "requirements assessment, not a funder decision"},
        "ranking": {"contract": "noesis-funding-shortlist-v1", "criteria": dict(DEFAULT_WEIGHTS)},
        "workflows": {
            "discovery": {"reuses": ["provider_execution.DurableHTTP", "DocumentStore", "SnapshotStore"],
                          "tools": ["funding_provider_contracts", "acquire_funding_source", "list_funding_opportunities",
                                    "inspect_funding_opportunity"]},
            "profile_to_shortlist": {"reuses": ["namespace/owner scopes"],
                                     "tools": ["create_funding_profile", "update_funding_profile", "inspect_funding_profile",
                                               "assess_funding_eligibility", "build_funding_shortlist", "inspect_funding_shortlist"]},
            "application_preparation": {"reuses": ["ResearchProjectStore", "AuthoredReportStore", "QuantitativeStore"],
                                        "tools": ["create_funding_workspace", "inspect_funding_workspace",
                                                  "update_funding_workspace_items", "refresh_funding_workspace",
                                                  "record_funding_workspace_outcome", "draft_funding_application",
                                                  "export_funding_application_draft"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_funding_monitor", "run_funding_monitor", "poll_funding_monitor"]},
        },
    },
    "never": ["submit applications", "contact funders", "guarantee eligibility or funding success"],
}
_DDL = """
CREATE TABLE IF NOT EXISTS funding_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _ensure(conn):
    conn.execute(_DDL)


def is_enabled(conn, namespace):
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='funding_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM funding_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Funding & Grants is disabled for this namespace; shared sources remain usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None):
    if "operator" not in scopes:
        raise BundleError("unauthorized", "operator scope is required to change bundle enablement")
    _ensure(conn)
    conn.execute(
        """INSERT INTO funding_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET
           enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms""",
        [namespace, bool(enabled), principal_id, (now or (lambda: int(time.time() * 1000)))()])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled),
            "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes):
    """Report ready / fixture-only / unavailable per provider and entry point."""
    from src.kb.funding_opportunities import FundingOpportunityStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "funding read scope is required")
    store = FundingOpportunityStore(conn, initialize=False)
    acquired = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='funding_provider_state'").fetchone()
    providers = {}
    for provider in PROVIDER_CONTRACTS:
        state = store.provider_state(namespace, provider) if acquired else {
            "provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state["stale"]:
            status = "unavailable" if state.get("last_execution") == "network" else "fixture-only"
        elif state.get("last_execution") == "network":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    discovery = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    local = "ready" if discovery != "unavailable" else "unavailable"
    return {
        "bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
        "providers": providers,
        "entry_points": {"discovery": discovery, "profile_to_shortlist": local,
                         "application_preparation": local, "monitoring": local},
        "composition_runtime": BUNDLE["architecture"]["status"],
    }
