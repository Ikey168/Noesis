"""The Society and population bundle: declaration, enablement, features, readiness and income evidence (IP11, #2637).

Composed under the pack/workflow composition contracts (``docs/architecture/pack-workflow-composition.md``): the
native manifest ``packs/society/manifest.json`` binds the ``society.income`` provider (income, poverty and
inequality series) plus the shared ``geospatial.core`` (place resolution), ``platform.entity-identity``,
``platform.subscriptions`` and ``platform.source-runtime`` providers. Demographics denominators and Labour series are
optional links: when ``economics.demographics`` or ``economics.labour`` is absent the links report
``provider_absent`` and nothing else degrades.

PIP, EU-SILC and OECD IDD coverage are three separate optional features (``pip``, ``eu-silc``, ``oecd-idd``; on by
default). Once the bundle is composition-managed the active plan decides which are selected and answers list the
others as ``features_disabled``; before cutover every feature is on and a per-namespace flag enables the bundle.

:func:`income_profile` is the cited answer for a place: each source's poverty and inequality figures as of a date,
side by side; :func:`export_profile` renders it as a ``noesis-evidence-bundle-v1`` in which every item is cited with
source, record revision (vintage) and as-of time.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.income_distribution_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDERS,
)
from src.kb.income_distribution_records import (
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    RECORD_TYPES,
    digest,
)

BUNDLE_ID = "society"
PROVIDER_ID = "society.income"
MANIFEST = "packs/society/manifest.json"
PROFILE_CONTRACT = "noesis-income-profile-v1"
FEATURES = {"pip": "pip", "eu-silc": "eurostat-silc", "oecd-idd": "oecd-idd"}
PROFILE_CONCEPTS = ("poverty_headcount", "gini")
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0", "domain": "society-population",
    "architecture": {"plan": "docs/architecture/pack-workflow-composition.md",
                     "status": "composed: manifest packs/society/manifest.json binds society.income plus shared "
                               "providers", "manifest": MANIFEST},
    "contributions": {
        "providers": [PROVIDER_ID],
        "sources": [{"provider": p, "access": c["access"], "status": c["status"],
                     "live_verification": LIVE_VERIFICATION[p]["status"], "bounded_coverage": BOUNDED_COVERAGE[p]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": "society-income-distribution", "version": "1.0.0",
                          "manifest": "config/source_packs/society.json"}],
        "ontology": {"contract": "noesis-income-distribution-record-v2", "record_types": list(RECORD_TYPES)},
        "optional_features": {feature: {"default": True, "provider": provider} for feature, provider in FEATURES.items()},
        "optional_links": {"economics.demographics": "population denominators by citation (provider_absent when "
                                                     "not installed)",
                           "economics.labour": "labour series for the same place (provider_absent when not "
                                               "installed)"},
        "minimisation": MINIMISATION,
    },
    "exclusions": list(EXCLUSIONS),
}
_DDL = """
CREATE TABLE IF NOT EXISTS society_bundle_state(
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


def _plan(coordinator) -> dict[str, Any]:
    return (coordinator.active() or {}).get("plan") or {}


def _selected(coordinator) -> bool:
    return any(p["id"] == BUNDLE_ID for p in _plan(coordinator).get("packs") or [])


def enabled_providers(conn) -> set[str]:
    """The source providers whose optional feature is selected (every one before composition management)."""
    coordinator = _coordinator(conn)
    if coordinator is None:
        return set(PROVIDERS)
    features = (_plan(coordinator).get("features") or {}).get(BUNDLE_ID)
    if features is None:
        return set(PROVIDERS)
    return {FEATURES[f] for f in features if f in FEATURES}


def is_enabled(conn, namespace) -> bool:
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='society_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM society_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace) -> None:
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Society is disabled; geospatial, demographics, labour and shared "
                                             "providers remain usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None) -> dict[str, Any]:
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
    conn.execute("INSERT INTO society_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes) -> dict[str, Any]:
    """ready / fixture-only / stale / unavailable / feature-disabled per source; live verification kept separate."""
    from src.kb.income_distribution_records import table_exists
    from src.kb.income_distribution_store import IncomeDistributionStore

    scopes = set(scopes)
    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "income read scope is required")
    store = IncomeDistributionStore(conn, initialize=False)
    selected = enabled_providers(conn)
    providers = {}
    for provider in PROVIDERS:
        if provider not in selected:
            providers[provider] = {"status": "feature-disabled", "live_verification": LIVE_VERIFICATION[provider]}
            continue
        state = store.provider_state(namespace, provider)
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state["stale"]:
            status = "stale"
        elif state.get("last_execution") == "live":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    links = {"economics.demographics": "available" if table_exists(conn, "demographic_series") else "provider_absent",
             "economics.labour": "available" if table_exists(conn, "labour_series") else "provider_absent"}
    return {"bundle": BUNDLE_ID, "provider": PROVIDER_ID, "namespace": namespace,
            "enabled": is_enabled(conn, namespace), "providers": providers, "optional_links": links,
            "entry_points": {"acquisition": acquisition,
                             "answers": "ready" if acquisition != "unavailable" else "unavailable"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION and "
                        "docs/development/income-distribution-evidence/",
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


def income_profile(conn, namespace: str, *, scopes: Iterable[str], as_of: Any = None, place_id: str | None = None,
                   area: Mapping[str, Any] | None = None,
                   concepts: Iterable[str] = PROFILE_CONCEPTS) -> dict[str, Any]:
    """A place's poverty and inequality figures from each source side by side as of a date, with links."""
    from src.kb.income_distribution_links import IncomeLinks
    from src.kb.income_distribution_queries import IncomeQueries
    from src.kb.income_distribution_records import table_exists

    scopes = set(scopes)
    queries = IncomeQueries(conn)
    selected = enabled_providers(conn)
    answers = {concept: queries.indicator_for_place(namespace, scopes=scopes, concept=concept, as_of=as_of,
                                                    place_id=place_id, area=area, enabled_providers=selected)
               for concept in concepts}
    series_ids = {r["series_id"] for a in answers.values() for r in a["results"]}
    links = [link for link in IncomeLinks(conn, initialize=False).links(namespace, scopes=scopes)
             if link["series_id"] in series_ids] if table_exists(conn, "income_links") else []
    first = next(iter(answers.values()))
    result = {
        "contract": PROFILE_CONTRACT, "namespace": namespace, "query": {"place_id": place_id, "area": area,
                                                                        "as_of": as_of, "concepts": list(concepts)},
        "as_of": first["as_of"], "answers": answers, "links": links,
        "none_on_record": [n for a in answers.values() for n in a["none_on_record"]],
        "features_disabled": first["features_disabled"], "exclusions": list(EXCLUSIONS),
        "minimisation": MINIMISATION["decision"],
    }
    result["profile_hash"] = digest(result)
    return result


def export_profile(profile: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """A ``noesis-evidence-bundle-v1``: every figure cited with source, record revision (vintage) and as-of time."""
    from src.evidence_bundle.builder import EvidenceBundleBuilder
    from src.kb.income_distribution_queries import cutoff_ms

    builder = EvidenceBundleBuilder("receipt", {"operation": PROFILE_CONTRACT, "query": profile["query"],
                                                "as_of": profile["as_of"]},
                                    created_at_ms=created_at_ms or 0, as_of_ms=cutoff_ms(profile["query"]["as_of"]))
    refs: list[str] = []
    for concept, answer in sorted(profile["answers"].items()):
        for row in answer["results"]:
            cite = row["citation"]
            object_id = f"income:{cite['series_id']}@{cite['vintage_id']}"
            builder.add_object("evidence", {
                "kind": "income-series-vintage", "concept": concept,
                "locator": {"cited": True, "document_id": cite["series_id"], "revision_id": cite["vintage_id"],
                            "url": cite.get("url")},
                "citation": {**cite, "source": cite["provider"], "record_revision": cite["vintage_id"]},
                "comparability_group": row["comparability_group"], "status": row["status"],
                "observations": [{k: o[k] for k in ("period", "value_text", "status", "estimation_type",
                                                    "survey_year", "income_reference_year", "flags")}
                                 for o in row["observations"]]}, object_id=object_id)
            refs.append(object_id)
            if cite.get("url"):
                builder.add_external_reference(f"source:{cite['series_id']}:{cite['vintage_id']}", cite["url"],
                                               required=False)
    for link in profile.get("links") or []:
        object_id = f"income-link:{link['link_id']}"
        builder.add_object("evidence", {"kind": "income-link", "basis": link["basis"], "state": link["state"],
                                        "locator": {"cited": False}, "record_revision_id": link["vintage_id"],
                                        "target": link["target"]}, object_id=object_id)
        refs.append(object_id)
    summary = {k: v for k, v in profile.items() if k not in {"answers", "links"}}
    builder.add_object("receipt", {"kind": "income-profile", **summary},
                       object_id=f"income-profile:{profile['profile_hash'][:24]}", references=refs, root=True)
    for gap in profile.get("none_on_record") or []:
        builder.add_omission(f"none on record: {gap['provider']} {gap['concept']}")
    for disabled in profile.get("features_disabled") or []:
        builder.add_omission(f"source {disabled['provider']}: {disabled['reason']}")
    for link in profile.get("links") or []:
        if link["state"] in {"provider_absent", "target_not_held", "unresolved"}:
            builder.add_omission(f"link {link['link_id']} {link['state']}")
    return builder.build()


__all__ = [
    "BUNDLE",
    "BUNDLE_ID",
    "FEATURES",
    "PROVIDER_ID",
    "BundleError",
    "enabled_providers",
    "export_profile",
    "income_profile",
    "is_enabled",
    "readiness",
    "require_enabled",
    "set_enabled",
]
