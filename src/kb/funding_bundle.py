"""The Funding & Grants bundle: declared contributions, enablement and readiness.

The bundle is composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``, C09.4): its manifest is
``packs/funding-grants/pack.json`` with ``composition.json``, it contributes the
``noesis.funding`` provider and binds the shared research-project, authored
report, quantitative calculation, subscription and document-reference
providers. It adds no scheduler, permission ledger, project store or
submission engine.

Enablement has one authority. Once the bundle is cut over to the composition
coordinator, :func:`set_enabled` is a selection change through the
coordinator and the selection is deployment-wide; before cutover (or with
``NOESIS_COMPOSITION_LIFECYCLE=legacy``) the per-namespace
``funding_bundle_state`` table is the legacy authority. The two are never
written for the same change.

Disabling the bundle blocks only the funding entry points. Shared providers
stay independently usable: ``DurableHTTP``, the document store, research
projects, authored reports, quantitative calculations and subscriptions are
never gated by this flag.
"""

from __future__ import annotations

import functools
import time

from src.ingestion.funding_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.funding_ranking import DEFAULT_WEIGHTS
from src.kb.funding_records import INSTRUMENT_KINDS, RECORD_KINDS, READ_SCOPE

BUNDLE_ID = "funding-grants"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "implemented: composed through packs/funding-grants (manifest, provider noesis.funding)",
        "manifest": "packs/funding-grants/composition.json",
        "provider": "config/composition/providers/funding.json",
        "depends_on": {"C02": "manifest and provider contracts", "C03": "resolved bindings",
                       "C04": "readiness assessment", "C05": "lifecycle and one enablement authority",
                       "C06": "source integration (not used: funding providers run through DurableHTTP)",
                       "C07": "authorized dispatch (no funding workflow template yet)"},
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


def _coordinator():
    """The composition coordinator when it is this bundle's enablement authority."""

    from src.composition import lifecycle

    coordinator = lifecycle.installed_coordinator()
    if coordinator is None or BUNDLE_ID not in coordinator.managed():
        return None
    return coordinator


def authority():
    return "composition" if _coordinator() is not None else "legacy"


def is_enabled(conn, namespace):
    coordinator = _coordinator()
    if coordinator is not None:
        return BUNDLE_ID in coordinator.store.selection()
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
    coordinator = _coordinator()
    if coordinator is not None:
        return _set_selected(coordinator, namespace, bool(enabled), principal_id=principal_id)
    _ensure(conn)
    conn.execute(
        """INSERT INTO funding_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET
           enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms""",
        [namespace, bool(enabled), principal_id, (now or (lambda: int(time.time() * 1000)))()])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled),
            "authority": "legacy", "scope": "namespace", "shared_capabilities_affected": []}


def _set_selected(coordinator, namespace, enabled, *, principal_id):
    """A composition selection change: select and activate, or disable the root."""

    selected = BUNDLE_ID in coordinator.store.selection()
    key = f"funding-enable:{enabled}:{principal_id}:{coordinator.now()}"
    receipt = None
    if enabled and not selected:
        version = max((m["version"] for m in coordinator.store.manifests() if m["name"] == BUNDLE_ID),
                      key=lambda v: tuple(int(x) for x in v.split(".")))
        coordinator.store.select(BUNDLE_ID, version, principal_id=principal_id)
        receipt = coordinator.activate(key, principal_id=principal_id)
    elif not enabled and selected:
        receipt = coordinator.disable(BUNDLE_ID, key, principal_id=principal_id)
    enabled_now = BUNDLE_ID in coordinator.store.selection()
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": enabled_now,
            "authority": "composition", "scope": "deployment",
            "receipt": None if receipt is None else {"status": receipt["status"],
                                                     "new_generation": receipt.get("new_generation")},
            "shared_capabilities_affected": []}


@functools.lru_cache(maxsize=1)
def _shipped_plan():
    """The bundle resolved against the shipped candidates (pure; cached)."""

    from src.composition.deployment import candidates
    from src.composition.resolver import resolve

    candidate_set = candidates()
    result = resolve(roots=[{"name": BUNDLE_ID, "range": BUNDLE["version"]}],
                     manifests=candidate_set["manifests"], providers=candidate_set["providers"],
                     contracts=candidate_set["contracts"])
    return result.get("plan"), candidate_set["providers"]


@functools.lru_cache(maxsize=1)
def _entry_points():
    """Entry point -> capabilities, from ``packs/funding-grants/composition.json``."""

    import json
    from pathlib import Path

    overlay = Path(__file__).resolve().parents[2] / "packs" / BUNDLE_ID / "composition.json"
    return json.loads(overlay.read_text(encoding="utf-8"))["metadata"]["entry_points"]


def composition_readiness(conn, *, scopes):
    """C04 readiness of the bundle's bindings, from the coordinator when it is the authority."""

    from src.composition.readiness import LIFECYCLE_BLOCKERS, assess

    coordinator = _coordinator()
    if coordinator is not None and coordinator.store.active_plan() is not None and any(
            m["name"] == BUNDLE_ID for m in coordinator.store.active_plan()["manifests"]):
        observed = coordinator.readiness(principal_id=None, scopes=scopes, conn=conn)
    else:
        plan, providers = _shipped_plan()
        if plan is None:
            return {"plan_digest": None, "authority": authority(), "operations": [], "entry_points": {}}
        observed = assess(plan, providers, conn=conn, scopes=scopes)
    operations = [
        {"capability": o["capability"], "provider_id": o["provider_id"], "state": o["state"],
         "blockers": [b["kind"] for b in o["blockers"]]}
        for o in observed["operations"] if o["consumer"].startswith(f"{BUNDLE_ID}@")
    ]
    blocked = {o["capability"] for o in operations if set(o["blockers"]) & LIFECYCLE_BLOCKERS}
    return {
        "plan_digest": observed["plan_digest"], "authority": authority(), "operations": operations,
        "entry_points": {name: ("unavailable" if blocked & set(capabilities) else "bound")
                         for name, capabilities in _entry_points().items()},
    }


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
    entry_points = {"discovery": discovery, "profile_to_shortlist": local,
                    "application_preparation": local, "monitoring": local}
    # Shared-provider readiness comes from the composition (C04): a lifecycle
    # blocker on a bound shared provider makes the entry points using it
    # unavailable; data readiness stays with the provider evidence above.
    composition = composition_readiness(conn, scopes=scopes)
    for name, state in composition["entry_points"].items():
        if state == "unavailable":
            entry_points[name] = "unavailable"
    return {
        "bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
        "authority": composition["authority"],
        "providers": providers,
        "entry_points": entry_points,
        "composition": composition,
        "composition_runtime": BUNDLE["architecture"]["status"],
    }
