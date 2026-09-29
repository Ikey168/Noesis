"""The Public Procurement bundle: declared contributions, enablement and readiness.

The bundle is composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): its composition
manifest is ``packs/procurement/manifest.json`` and its own provider is
``packs/procurement/providers/procurement.core.json``. It binds
``funding.core`` where the Funding & Grants profile, eligibility, ranking and
workspace machinery is reused, ``market.lei`` for LEI identity, and the
shared ``platform.*`` providers (research projects, authored reports,
subscriptions, source runtime, decisions, intake sessions). Once composition
managed, enablement is a selection change through the lifecycle coordinator
(the one authority) with an activation receipt. Before cutover the
per-namespace flag below is the authority.

Disabling the bundle blocks only the procurement entry points. Shared
providers and Funding & Grants stay independently usable. The bundle
implements no scheduler, permission ledger, project store or submission
engine; it never submits a bid and never contacts a buyer.
"""

from __future__ import annotations

import time

from src.ingestion.procurement_providers import LAST_LIVE_CHECK, LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.procurement_ranking import DEFAULT_WEIGHTS
from src.kb.procurement_records import CONTRACT as RECORD_CONTRACT
from src.kb.procurement_records import READ_SCOPE, STAGES

BUNDLE_ID = "procurement"
IMPLEMENTED = tuple(p for p, c in PROVIDER_CONTRACTS.items() if c["status"] == "implemented")
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/procurement/manifest.json binds procurement.core, funding.core, market.lei and shared platform providers",
        "manifest": "packs/procurement/manifest.json",
        "depends_on": {"C02": "contracts (src/composition/contracts.py)", "C03": "resolver (src/composition/resolver.py)",
                       "C04": "readiness (src/composition/readiness.py)", "C05": "lifecycle (src/composition/lifecycle.py)",
                       "C06": "source integration (src/ingestion/source_pack_runtime.py, config/source_packs/procurement.json)",
                       "C07": "authorized dispatch (src/composition/workflows.py)"},
        "depends_on_semantics": "resolved bindings, lifecycle and authorized dispatch",
    },
    "contributions": {
        "sources": [{"provider": p, "status": c["status"], "access": c.get("access"), "reason": c.get("reason"),
                     "live_verification": LIVE_VERIFICATION[p]["status"]} for p, c in PROVIDER_CONTRACTS.items()],
        "source_pack": {"pack_id": "procurement", "config": "config/source_packs/procurement.json",
                        "projector": "noesis-procurement-record-v1 -> src.kb.procurement_notices"},
        "ontology": {"contract": RECORD_CONTRACT, "stages": list(STAGES)},
        "reuses_funding": {"profiles": "src.kb.funding_profiles.FundingProfileStore (subclassed)",
                           "eligibility": "src.kb.funding_eligibility.assess (three-valued rules, unchanged)",
                           "ranking": "src.kb.funding_ranking structure and buckets",
                           "workspaces": "src.kb.funding_workspaces.FundingWorkspaceStore (subclassed checklist machinery)",
                           "monitoring": "src.kb.funding_monitoring delivery hints over SubscriptionStore"},
        "eligibility": {"contract": "noesis-procurement-eligibility-v1", "semantics": "requirements assessment per lot, not the buyer's decision"},
        "ranking": {"contract": "noesis-procurement-shortlist-v1", "criteria": dict(DEFAULT_WEIGHTS),
                    "semantics": "fit is an ordering aid, not a probability of winning"},
        "workflows": {
            "discovery": {"reuses": ["SourcePackRuntime (run_source_pack_execution, pack 'procurement')", "DocumentStore"],
                          "tools": ["procurement_provider_contracts", "list_procurement_notices", "inspect_procurement_notice",
                                    "procurement_notice_history"]},
            "profile_to_shortlist": {"reuses": ["FundingProfileStore", "funding eligibility engine", "namespace/owner scopes"],
                                     "tools": ["create_procurement_profile", "update_procurement_profile", "inspect_procurement_profile",
                                               "withdraw_procurement_profile", "assess_procurement_eligibility",
                                               "build_procurement_shortlist", "inspect_procurement_shortlist", "replay_procurement_shortlist"]},
            "award_history": {"reuses": ["EntityHistoryStore (identity decisions)", "canonical_entities", "LeiStore (market.lei)"],
                              "tools": ["procurement_award_history", "procurement_incumbency", "procurement_party_candidates",
                                        "decide_procurement_party_link", "revert_procurement_party_link"]},
            "bid_preparation": {"reuses": ["ResearchProjectStore", "AuthoredReportStore", "FundingWorkspaceStore"],
                                "tools": ["create_procurement_workspace", "inspect_procurement_workspace",
                                          "update_procurement_workspace_items", "refresh_procurement_workspace",
                                          "record_procurement_workspace_outcome", "draft_procurement_bid", "export_procurement_bid_draft"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)", "source-pack watermarks and schedules"],
                           "tools": ["create_procurement_monitor", "run_procurement_monitor", "poll_procurement_monitor"]},
        },
    },
    "never": ["submit bids", "contact buyers or procurement portals", "scrape portals without supported machine access",
              "guarantee eligibility or winning"],
}
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _coordinator(conn):
    """The lifecycle coordinator when Public Procurement is composition-managed, else None."""
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='composition_authority'").fetchone():
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator):
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn, namespace):
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='procurement_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM procurement_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Public Procurement is disabled for this namespace; shared sources and Funding & Grants remain usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None):
    if "operator" not in scopes:
        raise BundleError("unauthorized", "operator scope is required to change bundle enablement")
    stamp = (now or (lambda: int(time.time() * 1000)))()
    coordinator = _coordinator(conn)
    if coordinator is not None:
        key = f"procurement:{'enable' if enabled else 'disable'}:{principal_id}:{stamp}"
        if enabled:
            manifest = next(m for m in coordinator.installed("pack") if m["id"] == BUNDLE_ID)
            coordinator.select(BUNDLE_ID, f"^{manifest['version']}")
            receipt = coordinator.activate(key)
        elif _selected(coordinator):
            receipt = coordinator.disable(BUNDLE_ID, key)
        else:
            receipt = None
        return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": _selected(coordinator), "scope": "deployment",
                "authority": "composition-coordinator", "receipt": receipt, "shared_capabilities_affected": []}
    conn.execute(_DDL)
    conn.execute(
        """INSERT INTO procurement_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET
           enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms""",
        [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes):
    """ready / fixture-only / unavailable / not-implemented per provider and entry point; offline and live kept apart."""
    from src.kb.procurement_notices import ProcurementNoticeStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "procurement read scope is required")
    store = ProcurementNoticeStore(conn, initialize=False)
    acquired = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='procurement_provider_state'").fetchone()
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] != "implemented":
            providers[provider] = {"status": "not-implemented", "reason": contract["reason"], "live_verification": LIVE_VERIFICATION[provider]}
            continue
        state = store.provider_state(namespace, provider) if acquired else {"provider": provider, "last_success_ms": None,
                                                                            "stale": True, "reason": "never acquired"}
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state.get("last_execution") == "network" and not state["stale"]:
            status = "ready"
        elif state.get("last_execution") == "network":
            status = "unavailable"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    discovery = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    local = "ready" if discovery != "unavailable" else "unavailable"
    return {
        "bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace), "providers": providers,
        "entry_points": {"discovery": discovery, "profile_to_shortlist": local, "award_history": local,
                         "bid_preparation": local, "monitoring": local},
        "evidence": {"offline": "authored fixtures (tests/fixtures/source_packs/procurement-*.json); never live coverage",
                     "live": LAST_LIVE_CHECK},
        "composition_runtime": BUNDLE["architecture"]["status"],
        **_composition_readiness(conn, scopes),
    }


def _composition_readiness(conn, scopes):
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    return {"composition": assessment}
