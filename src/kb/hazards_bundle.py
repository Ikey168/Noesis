"""The Natural Hazards bundle: declared contributions, enablement, readiness and cited evidence export (NH13, #2366).

Composed under ``docs/architecture/pack-workflow-composition.md``: the manifest
``packs/natural-hazards/manifest.json`` binds ``hazards.core`` plus the shared
``geospatial.core``, ``osint.core``, ``platform.entity-identity``, ``platform.subscriptions``
and ``platform.source-runtime`` providers. Climate-environment links, weather warnings (#2163)
and the key-gated GloFAS source are optional features. Enablement is a selection change
through the lifecycle coordinator once composition-managed (a per-namespace flag
before). No scheduler, spatial store or entity store is added.

:func:`export_bundle` turns an events-for-place answer (and optionally the
alerts in force) into a ``noesis-evidence-bundle-v1`` in which every record is
cited with its source, the revision used and the as-of time, together with its
revision history and accepted correspondents.
"""

from __future__ import annotations

import time

from src.ingestion.hazard_sources import BOUNDED_COVERAGE, LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.hazards_records import NEVER, READ_SCOPE, RECORD_TYPES
from src.kb.hazards_store import iso, ms

BUNDLE_ID = "natural-hazards"
MANIFEST = "packs/natural-hazards/manifest.json"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/natural-hazards/manifest.json binds hazards.core plus shared providers",
        "manifest": MANIFEST,
        "depends_on": {"C02": "contracts (src/composition/contracts.py)", "C03": "resolver (src/composition/resolver.py)",
                       "C04": "readiness (src/composition/readiness.py)", "C05": "lifecycle (src/composition/lifecycle.py)",
                       "C06": "source integration (src/ingestion/source_pack_runtime.py)",
                       "C07": "authorized dispatch (src/composition/workflows.py)"},
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "access_decision": c["access_decision"],
                     "live_verification": LIVE_VERIFICATION[p]["status"], "coverage": BOUNDED_COVERAGE[p]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": "natural-hazards", "version": "1.0.0",
                          "manifest": "config/source_packs/natural-hazards.json"}],
        "ontology": {"contract": "noesis-hazard-record-v1", "record_types": list(RECORD_TYPES)},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (natural-hazards connector)", "HazardProjector"],
                            "tools": ["hazard_provider_contracts", "register_hazard_schemas"]},
            "place_events": {"reuses": ["GeospatialStore (places, boundaries, relations, receipts)"],
                             "tools": ["hazard_events_for_place", "hazard_event_revisions", "export_hazard_bundle"]},
            "alerts": {"reuses": ["published validity only"], "tools": ["hazard_alerts_in_force"]},
            "identity": {"reuses": ["EntityHistoryStore (match / non-match, undo)", "GeospatialStore relations"],
                         "tools": ["propose_hazard_correspondences", "review_hazard_correspondence",
                                   "revert_hazard_correspondence", "list_hazard_correspondences", "resolve_hazard_places"]},
            "links": {"reuses": ["DocumentStore documents (news, osint)", "environment records"],
                      "tools": ["discover_hazard_links", "list_hazard_links"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_hazard_monitor", "run_hazard_monitor", "poll_hazard_monitor"]},
        },
    },
    "never": list(NEVER),
}
_DDL = """
CREATE TABLE IF NOT EXISTS hazards_bundle_state(
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
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='hazards_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM hazards_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Natural Hazards is disabled; geospatial and shared providers remain usable")


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
    conn.execute("INSERT INTO hazards_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes):
    """fixture-only / ready / stale / unavailable / key-gated per provider; live verification kept separate."""

    from src.kb.hazards_links import linked_providers
    from src.kb.hazards_store import HazardStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "hazards read scope is required")
    tables = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='hazard_provider_state'").fetchone()
    store = HazardStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        state = store.provider_state(namespace, provider) if tables else {"acquired": False, "stale": True}
        if not state.get("acquired"):
            status = "key-gated (disabled by default)" if contract["access_decision"] == "key-gated" else "unavailable"
        elif state["stale"]:
            status = "stale"
        elif state.get("last_execution") == "network":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace), "providers": providers,
            "linked_providers": linked_providers(conn),
            "evidence": "offline fixture results and live results are reported separately; see "
                        "docs/development/hazards-evidence/",
            **_composition_readiness(conn, scopes)}


def _composition_readiness(conn, scopes):
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    assessment["omissions"] = [o for o in plan.get("omissions") or [] if o.get("pack") == BUNDLE_ID]
    return {"composition": assessment}


def export_bundle(answer, *, alerts=None, created_at_ms=None):
    """An evidence bundle for an events-for-place answer: every record cited with source, revision and as-of."""

    from src.evidence_bundle.builder import EvidenceBundleBuilder

    as_of = answer.get("as_of")
    builder = EvidenceBundleBuilder(
        "receipt", {"operation": "natural-hazards-place-events", "area": answer["area"], "window": answer["window"],
                    "as_of": as_of, "as_of_basis": answer.get("as_of_basis")},
        created_at_ms=created_at_ms, as_of_ms=ms(as_of) if as_of and as_of != "latest" else None)
    refs, cited = [], {}

    def cite(item_citation, role):
        object_id = f"hazard-record:{item_citation['record_id']}@{item_citation['revision_id']}"
        entry = cited.setdefault(object_id, {**item_citation, "as_of": as_of, "roles": []})
        if role not in entry["roles"]:
            entry["roles"].append(role)
        return object_id

    for event in answer["events"]:
        refs.append(cite(event["citation"], "revision in force at the as-of date"))
        for history in event["revision_history"]:
            object_id = f"hazard-revision:{event['record_id']}@{history['revision_id']}"
            builder.add_object("evidence", {"kind": "hazard-revision-history", "record_id": event["record_id"],
                                            "provider": event["provider"], **history}, object_id=object_id)
            refs.append(object_id)
        for other in event["correspondents"]:
            if other.get("citation"):
                refs.append(cite(other["citation"], f"accepted correspondent ({other['method']}); shown beside, not merged"))
    for alert in (alerts or {}).get("alerts") or []:
        refs.append(cite(alert["citation"], "alert or advisory in force at the requested time"))
    for object_id, entry in sorted(cited.items()):
        builder.add_object("evidence", {"kind": "hazard-record-revision", **entry}, object_id=object_id)
        builder.add_external_reference(f"source:{entry['record_id']}@{entry['revision_key']}", entry["source_url"],
                                       required=False)
    citations = [cited[k] for k in sorted(cited)]
    root = {"kind": "natural-hazards-answer", "answer": answer["answer"], "coverage": answer["coverage"],
            "events": [{"record_id": e["record_id"], "revision_id": e["revision_used"]["revision_id"]}
                       for e in answer["events"]],
            "alerts": [{"record_id": a["record_id"], "revision_id": a["citation"]["revision_id"]}
                       for a in (alerts or {}).get("alerts") or []],
            "exclusions": list(NEVER)}
    builder.add_object("receipt", root, object_id="natural-hazards-answer", references=refs, root=True)
    for provider, state in sorted(answer["coverage"].items()):
        if state != "covered":
            builder.add_omission(f"{provider}: {state}")
    if answer.get("truncated"):
        builder.add_omission("results truncated at the bounded result cap")
    bundle = builder.build()
    return {"bundle": bundle, "citations": citations, "as_of": as_of or "latest", "exported_at": iso(bundle["created_at_ms"]),
            "exclusions": list(NEVER)}
