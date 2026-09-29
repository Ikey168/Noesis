"""The Humanitarian Response and Conflict Events bundle: declaration, enablement, readiness and dossiers (HR12, #2283).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): the manifest is
``packs/humanitarian/manifest.json`` binding ``humanitarian.core`` plus the
shared ``geospatial.core``, ``osint.core``, ``news.core``,
``economics.demographics`` and ``platform.*`` providers. ACLED acquisition is
the optional ``acled`` feature, gated on the HR06 licence decision (declined).
Enablement is a selection change through the lifecycle coordinator once the
bundle is composition-managed; before cutover a per-namespace flag applies.
It adds no scheduler, entity store or spatial store.

A *dossier* is the cited answer for a place or crisis as of a date (reports,
appeals, crises, dataset revisions), the conflict events of a bounded area and
window with their precision and history, the identity state and the citation
links; :func:`export_dossier` renders it as a ``noesis-evidence-bundle-v1`` in
which every record revision is cited with source, revision and as-of time.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.humanitarian_sources import ACLED_DECISION, LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.humanitarian_records import EXCLUSIONS, READ_SCOPE, RECORD_TYPES, digest

BUNDLE_ID = "humanitarian"
MANIFEST = "packs/humanitarian/manifest.json"
DOSSIER_CONTRACT = "noesis-humanitarian-dossier-v1"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/humanitarian/manifest.json binds humanitarian.core plus shared providers",
        "manifest": MANIFEST,
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "status": c["status"],
                     "live_verification": LIVE_VERIFICATION[p]["status"]} for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": "humanitarian-response", "version": "1.0.0",
                          "manifest": "config/source_packs/humanitarian.json"}],
        "ontology": {"contract": "noesis-humanitarian-record-v1", "record_types": list(RECORD_TYPES)},
        "optional_features": {"acled": {"default": False, "decision": ACLED_DECISION}},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (humanitarian connector)", "DocumentStore"],
                            "tools": ["humanitarian_source_contracts", "acquire_humanitarian_source"]},
            "dossier": {"reuses": ["GeospatialStore (admin places, geometries)", "evidence bundle builder"],
                        "tools": ["humanitarian_dossier", "export_humanitarian_dossier",
                                  "query_humanitarian_conflict_events", "humanitarian_event_history"]},
            "identity": {"reuses": ["EntityHistoryStore", "canonical_entities", "GeospatialStore"],
                         "tools": ["propose_humanitarian_identity", "review_humanitarian_identity",
                                   "revert_humanitarian_identity", "list_humanitarian_identity"]},
            "links": {"reuses": ["documents", "event_dossier_revisions", "demographic_series/vintages"],
                      "tools": ["cite_humanitarian_link", "attach_humanitarian_population", "list_humanitarian_links"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_humanitarian_monitor", "run_humanitarian_monitor",
                                     "poll_humanitarian_monitor"]},
        },
    },
    "exclusions": list(EXCLUSIONS),
}
_DDL = """
CREATE TABLE IF NOT EXISTS humanitarian_bundle_state(
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
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='humanitarian_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM humanitarian_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Humanitarian is disabled; geospatial, news, OSINT, demographics and "
                                             "shared providers remain usable")


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
    conn.execute("INSERT INTO humanitarian_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes):
    """ready / fixture-only / stale / unavailable / declined per provider; live verification kept separate."""

    from src.kb.humanitarian_store import HumanitarianStore

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "humanitarian read scope is required")
    store = HumanitarianStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] == "declined":
            providers[provider] = {"status": "declined", "reason": ACLED_DECISION["query_notice"],
                                   "decision": ACLED_DECISION["reference"], "live_verification": LIVE_VERIFICATION[provider]}
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
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace), "providers": providers,
            "entry_points": {"acquisition": acquisition,
                             "dossier": "ready" if acquisition != "unavailable" else "unavailable"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION and "
                        "docs/development/humanitarian-evidence/",
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


def dossier(conn, namespace: str, *, as_of: Any, scopes: Iterable[str], pcode: str | None = None,
            place_id: str | None = None, crisis_key: str | None = None,
            events: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The cited dossier for a place or crisis as of a date, with bounded conflict events and their history."""

    from src.kb.humanitarian_identity import HumanitarianIdentity
    from src.kb.humanitarian_links import HumanitarianLinks
    from src.kb.humanitarian_queries import HumanitarianQueries

    scopes = set(scopes)
    queries = HumanitarianQueries(conn)
    published = queries.published_about(namespace, as_of=as_of, scopes=scopes, pcode=pcode, place_id=place_id,
                                        crisis_key=crisis_key)
    conflict = None
    if events:
        conflict = queries.conflict_events(namespace, scopes=scopes, as_of=as_of, **dict(events))
        for rows in conflict["by_coder"].values():
            for event in rows:
                event["history"] = [{"coding_status": r["coding_status"], "dataset_version": r["dataset_version"],
                                     "revision_id": r["revision_id"], "as_of": r["as_of"],
                                     "changed_fields": [c["field"] for c in r["changed_fields"]]}
                                    for r in queries.event_history(namespace, event["record_key"], scopes=scopes)["revisions"]
                                    if r["as_of"] is None or as_of is None or r["as_of"] <= published["as_of"]]
    identity = HumanitarianIdentity(conn, initialize=False)
    keys = {i["record_key"] for i in published["items"]}
    links = HumanitarianLinks(conn, initialize=False).links(namespace, scopes=scopes) if \
        conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='humanitarian_links'").fetchone() else \
        {"links": [], "broken": []}
    result = {
        "contract": DOSSIER_CONTRACT, "namespace": namespace, "as_of": published["as_of"], "query": published["query"],
        "published": published, "conflict_events": conflict,
        "identity": {"unmatched": identity.unmatched(namespace, scopes=scopes),
                     "boundary": published.get("boundary")},
        "links": [link for link in links["links"] if link["record_key"] in keys],
        "broken_links": [link for link in links["broken"] if link["record_key"] in keys],
        "sources_consulted": published["sources_consulted"], "sources_declined": published["sources_declined"],
        "none_on_record": published["none_on_record"] + ((conflict or {}).get("none_on_record") or []),
        "exclusions": list(EXCLUSIONS),
    }
    result["dossier_hash"] = digest({k: v for k, v in result.items() if k != "identity"})
    return result


def export_dossier(dossier_value: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """A ``noesis-evidence-bundle-v1``: every record revision cited with source, revision and as-of time."""

    from src.evidence_bundle.builder import EvidenceBundleBuilder
    from src.kb.humanitarian_records import to_ms

    builder = EvidenceBundleBuilder("receipt", {"operation": DOSSIER_CONTRACT, "query": dossier_value["query"],
                                                "as_of": dossier_value["as_of"]},
                                    created_at_ms=created_at_ms or 0, as_of_ms=to_ms(dossier_value["as_of"]))
    refs: list[str] = []
    events = [e for rows in ((dossier_value.get("conflict_events") or {}).get("by_coder") or {}).values() for e in rows]
    for item in list(dossier_value["published"]["items"]) + events:
        cite = item["citation"]
        object_id = f"humanitarian:{cite['record_key']}@{cite['revision_id']}"
        builder.add_object("evidence", {"kind": "humanitarian-record-revision",
                                        "locator": {"cited": True, "document_id": cite["record_key"],
                                                    "revision_id": cite["revision_id"], "url": cite.get("source_url")},
                                        "citation": cite}, object_id=object_id)
        refs.append(object_id)
        if cite.get("source_url"):
            builder.add_external_reference(f"source:{cite['record_key']}", cite["source_url"], required=False)
    for link in dossier_value.get("links") or []:
        object_id = f"humanitarian-link:{link['link_id']}"
        builder.add_object("evidence", {"kind": "citation-link", "basis": link["basis"],
                                        "locator": {"cited": False},
                                        "record_revision_id": link["record_revision_id"],
                                        "target": {"kind": link["target_kind"], "id": link["target_id"],
                                                   "revision": link["target_revision"]}}, object_id=object_id)
        refs.append(object_id)
    summary = {k: v for k, v in dossier_value.items() if k not in {"published", "conflict_events", "links"}}
    builder.add_object("receipt", {"kind": "humanitarian-dossier", **summary}, object_id=f"humanitarian-dossier:"
                       f"{dossier_value['dossier_hash'][:24]}", references=refs, root=True)
    for gap in dossier_value.get("none_on_record") or []:
        builder.add_omission("none on record: " + ", ".join(f"{k}={v}" for k, v in sorted(gap.items())))
    for declined in dossier_value.get("sources_declined") or []:
        builder.add_omission(f"source {declined['source']} {declined['reason']}")
    for link in dossier_value.get("broken_links") or []:
        builder.add_omission(f"broken link {link['link_id']}: {link['reason']}")
    return builder.build()
