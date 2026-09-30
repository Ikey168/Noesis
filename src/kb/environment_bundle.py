"""The Climate and Environment bundle: declared contributions, enablement and readiness (E12).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): the manifest is
``packs/climate-environment/manifest.json`` binding ``environment.core`` plus
the shared ``geospatial.core``, ``geospatial.transit``, ``market.lei`` and
``platform.*`` providers. Enablement is a selection change through the
lifecycle coordinator (one authority, with an activation receipt) once the
bundle is composition-managed; before cutover a per-namespace flag applies.
It adds no scheduler, permission ledger, project store or spatial store.

Disabling the bundle blocks only the environment entry points. Geospatial
places, features, WFS acquisition, transit, LEI and the platform providers
keep serving every other root.
"""

from __future__ import annotations

import time

from src.ingestion.environment_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.environment_records import KINDS, READ_SCOPE, RECORD_TYPES

BUNDLE_ID = "climate-environment"
MANIFEST = "packs/climate-environment/manifest.json"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "replaces": "the retired 'energy' example pack (keyword-only outage panel) with real grid records",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/climate-environment/manifest.json binds environment.core plus shared providers",
        "manifest": MANIFEST,
        "depends_on": {"C02": "contracts (src/composition/contracts.py)", "C03": "resolver (src/composition/resolver.py)",
                       "C04": "readiness (src/composition/readiness.py)", "C05": "lifecycle (src/composition/lifecycle.py)",
                       "C06": "source integration (src/ingestion/source_pack_runtime.py, source_pack_upgrades.py)",
                       "C07": "authorized dispatch (src/composition/workflows.py)"},
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": "climate-environment", "version": "1.0.0",
                          "manifest": "packs/climate-environment/source_packs/climate-environment.json"},
                         {"pack_id": "geospatial-berlin", "version": "1.2.0", "upgrade_of": "1.1.0",
                          "manifest": "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json"}],
        "ontology": {"contract": "noesis-environment-record-v1", "record_types": list(RECORD_TYPES), "kinds": list(KINDS)},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (environment connector)", "DurableHTTP", "DocumentStore",
                                       "WFS 2.0.0 adapter via geospatial-berlin upgrade"],
                            "tools": ["environment_provider_contracts", "acquire_environment_source"]},
            "place_dossier": {"reuses": ["GeospatialStore (places, relations, resolver)",
                                         "GeospatialFeatureStore (within, replay)"],
                              "tools": ["build_environment_place_dossier", "inspect_environment_dossier",
                                        "replay_environment_dossier", "export_environment_dossier",
                                        "lookup_environment_series", "list_environment_grid_events",
                                        "list_environment_facilities"]},
            "vintages": {"reuses": ["economic provider-vintage clock/basis pattern"],
                         "tools": ["compare_environment_vintages"]},
            "identity_and_obligations": {"reuses": ["src.kb.entities", "src.kb.lei", "policy-monitor assertions"],
                                         "tools": ["propose_environment_operator_links", "review_environment_operator_link",
                                                   "attach_environment_obligation_evidence",
                                                   "inspect_environment_obligation_evidence"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)",
                                      "maintenance orchestrator watermarks"],
                           "tools": ["create_environment_monitor", "run_environment_monitor", "poll_environment_monitor"]},
            # The optional ``biodiversity`` feature (environment.biodiversity, default off; #2220).
            "biodiversity": {
                "feature": "biodiversity",
                "source_pack": "packs/climate-environment/source_packs/climate-environment-biodiversity.json",
                "reuses": ["SourcePackRuntime (biodiversity connector)", "GeospatialStore (places, contains receipts)",
                           "canonical_entities and EntityHistoryStore (taxon identity decisions)",
                           "DocumentStore papers (citations by DOI)", "SubscriptionStore (biodiversity monitors)",
                           "SchemaRegistry"],
                "tools": ["biodiversity_source_contracts", "biodiversity_readiness", "lookup_taxa",
                          "occurrences_for_taxon_or_place", "conservation_status_history",
                          "propose_taxon_identity_matches", "review_taxon_identity_match",
                          "link_biodiversity_records", "export_biodiversity_bundle", "create_biodiversity_monitor",
                          "run_biodiversity_monitor"],
                "never": ["model species distributions", "estimate abundance or presence/absence",
                          "de-generalise sensitive-species locations", "derive a threat status"]},
            # The environment.water provider with its optional water-pegelonline, water-usgs and water-eea-wise
            # features (default off; #2582).
            "water": {
                "features": ["water-pegelonline", "water-usgs", "water-eea-wise"],
                "source_pack": "packs/climate-environment/source_packs/climate-environment-water.json",
                "reuses": ["SourcePackRuntime (water connector)", "GeospatialStore (places, contains receipts)",
                           "canonical_entities and EntityHistoryStore (station and water-body match decisions)",
                           ("hazard records, documents, weather station identifiers and infrastructure assets "
                           "(links, reported unavailable when absent)"), "SubscriptionStore (water monitors)",
                           "SchemaRegistry"],
                "tools": ["water_source_contracts", "water_readiness", "water_value_at", "water_series",
                          "water_station_history", "water_for_place", "water_body_status_history",
                          "propose_water_identity_matches", "review_water_identity_match", "link_water_records",
                          "export_water_bundle", "create_water_monitor", "run_water_monitor"],
                "never": ["forecast floods", "interpolate or fill gaps", "assess water-body status",
                          "score flood risk"]},
        },
    },
    "never": ["infer an attribution claim", "produce climate projections", "make compliance determinations",
              "present forecasts or reanalysis as observations", "infer outages from news",
              "assert legal thresholds that are not cited from a source"],
}
_DDL = """
CREATE TABLE IF NOT EXISTS environment_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


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
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='environment_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM environment_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Climate and Environment is disabled; geospatial and shared providers remain usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None):
    if "operator" not in scopes:
        raise BundleError("unauthorized", "operator scope is required to change bundle enablement")
    stamp = (now or (lambda: int(time.time() * 1000)))()
    coordinator = _coordinator(conn)
    if coordinator is not None:
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
    conn.execute(_DDL)
    conn.execute("INSERT INTO environment_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes):
    """ready / fixture-only / unavailable per provider, plus live-verification state (kept separate)."""

    from src.kb.environment_store import EnvironmentStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "environment read scope is required")
    store = EnvironmentStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract.get("status") == "not implemented":
            providers[provider] = {"status": "not implemented", "reason": contract["reason"],
                                   "live_verification": LIVE_VERIFICATION[provider]}
            continue
        state = store.provider_state(namespace, provider)
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state["stale"]:
            status = "stale"
        elif state.get("last_execution") == "network":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers, "entry_points": {"acquisition": acquisition,
                                                     "place_dossier": "ready" if acquisition != "unavailable" else "unavailable"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION",
            **_composition_readiness(conn, scopes)}


def _composition_readiness(conn, scopes):
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    return {"composition": assessment}


def biodiversity_section(conn, namespace, *, scopes, taxon=None, place_id=None, as_of=None):
    """The optional biodiversity feature's part of a place or taxon bundle: occurrences as of a date and, for a
    taxon, the conservation status history reduced to citation level for export (IUCN reference-only)."""

    from src.kb.biodiversity_queries import bundle

    require_enabled(conn, namespace)
    return bundle(conn, namespace, scopes=scopes, taxon=taxon, place_id=place_id, as_of=as_of)


def water_section(conn, namespace, *, scopes, place_id=None, station=None, water_body=None, as_of=None):
    """The environment.water provider's part of a place, station or water-body bundle: every item cites source,
    record revision and as-of time; no personal fields."""

    from src.kb.water_queries import bundle

    require_enabled(conn, namespace)
    return bundle(conn, namespace, scopes=scopes, place_id=place_id, station=station, water_body=water_body,
                  as_of=as_of)
