"""The Clinical Evidence bundle: declared contributions, enablement and readiness (H12).

The bundle is a native composition manifest (``packs/clinical-evidence/manifest.json``)
binding its own provider ``clinical.core`` plus shared providers:
``science.literature`` (literature claims), ``science.methodology``,
``science.systematic-reviews`` and ``science.paper-families`` (Science owners
this pack extends rather than duplicates), and ``platform.source-acquisition``
and ``platform.subscriptions``. Its sources run as the ``clinical-evidence``
source pack in the shared runtime.

Enablement has one authority: a selection change through the lifecycle
coordinator, with the coordinator's activation receipt. Disabling Clinical
Evidence removes only its entry points; Science and every shared provider keep
serving their other consumers. No scheduler, permission ledger, project store,
paper store or database is added.
"""

from __future__ import annotations

import time

from src.ingestion.clinical_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.clinical_evidence import DESIGN_RULES, NON_ADVICE, SUMMARY_RULES
from src.kb.clinical_records import CONTRACT, READ_SCOPE, RECORD_KINDS

BUNDLE_ID = "clinical-evidence"
SOURCE_PACK = "clinical-evidence"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/clinical-evidence/manifest.json binds clinical.core plus Science and "
                  "platform providers",
        "manifest": "packs/clinical-evidence/manifest.json",
        "source_pack": "config/source_packs/clinical-evidence.json",
    },
    "contributions": {
        "sources": [{"provider": p, "status": c["status"], "access": c.get("access"),
                     "live_verification": LIVE_VERIFICATION[p]} for p, c in PROVIDER_CONTRACTS.items()],
        "records": {"contract": CONTRACT, "record_kinds": list(RECORD_KINDS)},
        "strength_view": {"contract": "noesis-clinical-strength-view-v1", "design_rules": DESIGN_RULES,
                          "summary_rules": SUMMARY_RULES, "score": None},
        "workflows": {
            "acquisition": {"reuses": ["source-pack runtime (clinical-evidence pack)", "DocumentStore"],
                            "tools": ["noesis-knowledge-engine.run_source_pack_execution", "clinical_provider_contracts",
                                      "import_prospero_registration"]},
            "trials": {"reuses": ["ClinicalRecordStore"], "tools": ["lookup_clinical_trial", "clinical_trial_history"]},
            "publications": {"reuses": ["Science paper documents", "PaperFamilyStore", "Crossref notices"],
                             "tools": ["link_clinical_publications", "review_clinical_publication_link",
                                       "clinical_coverage_gaps"]},
            "methodology": {"reuses": ["MethodologyStore"], "tools": ["record_clinical_trial_design",
                                                                       "clinical_outcome_switching"]},
            "terms": {"reuses": ["OntologyAlignmentStore (MeSH crosswalks)"],
                      "tools": ["align_clinical_terms", "expand_clinical_question"]},
            "evidence_map": {"reuses": ["evidence bundle builder"],
                             "tools": ["build_clinical_evidence_map", "inspect_clinical_evidence_map",
                                       "clinical_strength_view", "export_clinical_evidence_bundle"]},
            "monitoring": {"reuses": ["SubscriptionStore", "source-pack schedules"],
                           "tools": ["create_clinical_monitor", "run_clinical_monitor", "poll_clinical_monitor"]},
            # The optional ``surveillance`` feature (clinical.surveillance, default off; #1917).
            "surveillance": {"reuses": ["source-pack runtime (clinical-evidence pack, surveillance connector)",
                                        "SDMXConnector (Eurostat)", "GenesisConnector (Destatis)",
                                        "OntologyAlignmentStore (MeSH and ICD crosswalks)",
                                        "GeospatialFeatureStore (boundaries and place resolution)",
                                        "environment_vintages comparison and pin status", "PublicationLinker",
                                        "SubscriptionStore (clinical monitors)"],
                             "feature": "surveillance",
                             "tools": ["surveillance_readiness", "list_surveillance_series",
                                       "surveillance_series_values", "expand_surveillance_condition",
                                       "surveillance_boundary_series", "compare_surveillance_vintages",
                                       "surveillance_reporting_delay", "link_surveillance_series",
                                       "create_surveillance_monitor", "run_surveillance_monitor"]},
            # The optional ``medicines`` feature (clinical.medicines, default off; #2214).
            "medicines": {"reuses": ["source-pack runtime (clinical-evidence pack, medicines connector)",
                                     "OpenfdaAdapter (Drugs@FDA submissions)", "ClinicalRecordStore",
                                     "clinical term crosswalks (RxNorm identity review)", "PublicationLinker",
                                     "SubscriptionStore (clinical monitors)"],
                          "feature": "medicines",
                          "tools": ["medicines_readiness", "medicine_status_as_of", "medicine_label_as_of",
                                    "compare_medicine_labels", "medicine_safety_communications",
                                    "medicine_regulatory_timeline", "propose_medicine_identities",
                                    "review_medicine_identity", "link_medicine_evidence",
                                    "create_medicines_monitor", "run_medicines_monitor"]},
            # The optional ``health-capacity`` feature (clinical.health-capacity, default off; #2215).
            "health_capacity": {"reuses": ["source-pack runtime (clinical-evidence pack, health-capacity connector "
                                           "= the surveillance adapter)", "SDMXConnector (OECD, Eurostat)",
                                           "SurveillanceStore (series, definitions, vintages)",
                                           "surveillance_vintages comparison", "GeospatialStore (place resolution)",
                                           "Economics series map (read-only)", "SubscriptionStore (clinical monitors)"],
                                "feature": "health-capacity",
                                "tools": ["health_capacity_readiness", "health_capacity_as_of",
                                          "health_capacity_definition_history", "health_capacity_comparability",
                                          "resolve_health_capacity_places", "propose_health_capacity_mapping",
                                          "record_health_capacity_note", "health_capacity_beside_surveillance",
                                          "link_health_capacity_economics", "create_health_capacity_monitor",
                                          "run_health_capacity_monitor"]},
            # The clinical.devices provider (#2654): optional medical-devices-fda, -gudid and -eudamed features
            # (default off), records in src/kb/medical_devices_records.py.
            "medical_devices": {"reuses": ["source-pack runtime (clinical-evidence pack, medical-devices connector)",
                                           "EntityHistoryStore (identity decisions)", "OwnershipStore (manufacturers)",
                                           "Product safety notice tables (recall links)",
                                           "ClinicalRecordStore (Medicines and trial links)",
                                           "SubscriptionStore (device monitors)"],
                                "features": ["medical-devices-fda", "medical-devices-gudid",
                                             "medical-devices-eudamed"],
                                "tools": ["medical_devices_readiness", "medical_device_regulatory_history",
                                          "medical_device_adverse_event_counts",
                                          "export_medical_device_evidence_bundle",
                                          "propose_medical_device_identities", "review_medical_device_identity",
                                          "link_medical_device_records", "create_medical_device_monitor",
                                          "run_medical_device_monitor"]},
        },
    },
    "boundary": NON_ADVICE,
    "never": ["give medical advice", "recommend a dose or a treatment", "assert a GRADE without its inputs",
              "treat adverse-event reports as incidence or causation", "merge records across registries"],
}


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _coordinator(conn):
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='composition_authority'").fetchone():
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator):
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn, namespace):
    """Deployment-wide selection when composition-managed; available by default otherwise."""
    del namespace
    coordinator = _coordinator(conn)
    return True if coordinator is None else _selected(coordinator)


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Clinical Evidence is not selected; Science and shared providers remain "
                                             "usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None):
    """Enable or disable through the lifecycle coordinator (one authority, activation receipt)."""
    if "operator" not in scopes:
        raise BundleError("unauthorized", "operator scope is required to change bundle enablement")
    coordinator = _coordinator(conn)
    if coordinator is None:
        raise BundleError("not_composition_managed", "install and cut over packs/clinical-evidence/manifest.json; "
                                                     "enablement is a coordinator selection change")
    stamp = (now or (lambda: int(time.time() * 1000)))()
    key = f"{BUNDLE_ID}:{'enable' if enabled else 'disable'}:{principal_id}:{stamp}"
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


def readiness(conn, namespace, *, scopes):
    """ready / fixture-only / unavailable / not-implemented per provider, plus entry points."""
    from src.kb.clinical_records import ClinicalRecordStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "clinical read scope is required")
    acquired = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='clinical_provider_state'"
                            ).fetchone()
    store = ClinicalRecordStore(conn, initialize=False) if acquired else None
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] != "implemented":
            providers[provider] = {"status": contract["status"], "reason": contract.get("reason"),
                                   "live_verification": LIVE_VERIFICATION[provider]}
            continue
        state = store.provider_state(namespace, provider) if store else {
            "provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state.get("last_execution") == "network":
            status = "unavailable" if state["stale"] else "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    local = "ready" if acquisition != "unavailable" else "unavailable"
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers,
            "entry_points": {"acquisition": acquisition, "trials": local, "publications": local, "methodology": local,
                             "terms": local, "evidence_map": local, "monitoring": local},
            "boundary": NON_ADVICE, **_composition_readiness(conn, scopes)}


def _composition_readiness(conn, scopes):
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    return {"composition": assessment}
