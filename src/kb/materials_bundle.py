"""The Materials bundle: declared contributions, enablement and readiness (MT12, #2090).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): the v1 manifest
``packs/materials/pack.json`` plus the ``packs/materials/composition.json``
overlay bind ``materials.computed`` (the property record store, lookups,
comparison, search and release tracking) and ``materials.experimental`` (the
cited dossier: papers by DOI and test-method standards by designation); the
optional ``phase-identity`` feature adds ``materials.structures`` (reviewable
phase-level identity through ``ownership.core``'s shared state machine). It
reuses the
shared ``science.literature``, ``technology.standards``,
``platform.source-runtime``, ``platform.subscriptions`` and
``platform.entity-identity`` providers. Enablement belongs to the lifecycle
coordinator once the bundle is composition-managed; the pack adds no
scheduler, permission ledger, project store or entity store.
"""

from __future__ import annotations

from src.ingestion.materials_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.materials_records import METHOD_CLASSES, PROVIDERS, READ_SCOPE
from src.kb.materials_units import PROPERTIES

BUNDLE_ID = "materials"
MANIFEST = "packs/materials/pack.json"
EXCLUSIONS = [
    "closed commercial databases and handbooks (MatWeb, Granta, Springer Materials, ASM handbooks)",
    "material selection recommendations, suitability-for-use or design-allowable claims",
    "property prediction or ML-estimated values produced by Noesis",
    "unrestricted mirroring of source databases; structure files beyond bounded, licensed fetches",
]
BUNDLE = {
    "bundle": BUNDLE_ID,
    "version": "1.0.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "manifest": MANIFEST,
        "overlay": "packs/materials/composition.json",
    },
    "contributions": {
        "sources": [
            {
                "provider": p,
                "decision": PROVIDER_CONTRACTS[p]["decision"],
                "live_verification": LIVE_VERIFICATION[p]["status"],
            }
            for p in PROVIDERS
        ],
        "source_packs": [
            {
                "pack_id": "materials",
                "version": "1.0.0",
                "manifest": "config/source_packs/materials.json",
            }
        ],
        "ontology": {
            "contract": "noesis-material-record-v1",
            "properties": sorted(PROPERTIES),
            "method_classes": list(METHOD_CLASSES),
        },
    },
    "never": EXCLUSIONS
    + [
        "merge or average computed and measured values",
        "present a computed value as a measurement",
    ],
}


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _coordinator(conn):
    if not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='composition_authority'"
    ).fetchone():
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator):
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn):
    coordinator = _coordinator(conn)
    return True if coordinator is None else _selected(coordinator)


def require_enabled(conn):
    if not is_enabled(conn):
        raise BundleError(
            "bundle_disabled",
            "the Materials bundle is not selected; shared providers remain usable",
        )


def phase_identity_enabled(conn):
    """The optional ``phase-identity`` feature: always available before cutover, else as selected in the plan."""

    coordinator = _coordinator(conn)
    if coordinator is None:
        return True
    plan = (coordinator.active() or {}).get("plan") or {}
    return _selected(coordinator) and "phase-identity" in (
        (plan.get("features") or {}).get(BUNDLE_ID) or []
    )


def require_phase_identity(conn):
    if not phase_identity_enabled(conn):
        raise BundleError(
            "feature_disabled",
            "the Materials phase-identity feature is not selected; records are "
            "compared per record only",
        )


def readiness(conn, namespace, *, scopes):
    """Per provider: unavailable (never acquired), stale, fixture-only or ready; live verification kept separate."""

    from src.kb.materials_store import MaterialsStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", f"{READ_SCOPE} is required")
    store = MaterialsStore(conn, initialize=False)
    providers = {}
    for provider in PROVIDERS:
        state = store.provider_state(namespace, provider)
        status = (
            "unavailable"
            if "last_success_ms" not in state
            else "stale"
            if state["stale"]
            else "ready"
            if state.get("last_execution") == "network"
            else "fixture-only"
        )
        providers[provider] = {
            "status": status,
            "state": state,
            "live_verification": LIVE_VERIFICATION[provider],
        }
    return {
        "bundle": BUNDLE_ID,
        "namespace": namespace,
        "enabled": is_enabled(conn),
        "stores_ready": store.ready(),
        "providers": providers,
        "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION",
    }
