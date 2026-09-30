"""The Corporate Ownership and Registries bundle: declaration, acquisition, enablement and readiness (O11).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): the manifest is
``packs/corporate-ownership/manifest.json`` and the bundle's own provider is
``packs/corporate-ownership/providers/ownership.core.json``. Everything else is
shared: ``market.lei`` (LEI records), ``platform.entity-identity`` (canonical
entities and identity decisions), ``platform.source-runtime`` (acquisition
runs, cursors, budgets, receipts) and ``platform.authored-reports`` (dossier
export). Once cut over, enablement is a coordinator selection change with an
activation receipt; before cutover a per-namespace flag is the authority. No
scheduler, permission ledger, project store, entity store or database is added.

Disabling blocks only the ownership entry points; the shared providers keep
serving every other bundle.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.ingestion.ownership_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.ownership_records import ASSERTION_KINDS, KINDS, READ_SCOPE, WRITE_SCOPE
from src.kb.ownership_store import OwnershipError, OwnershipStore, authorize

BUNDLE_ID = "corporate-ownership"
SOURCE_PACK_ID = "corporate-ownership"
SOURCE_PACK_PATH = Path(__file__).resolve().parents[2] / "config/source_packs/corporate-ownership.json"
INGEST_SCOPE = "knowledge:ingestion:execute"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/corporate-ownership/manifest.json binds ownership.core plus market.lei, "
                  "platform.entity-identity, platform.source-runtime and platform.authored-reports",
        "manifest": "packs/corporate-ownership/manifest.json",
        "source_pack": "config/source_packs/corporate-ownership.json",
    },
    "contributions": {
        "sources": [{"provider": p, "status": c["status"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "records": {"contract": "noesis-ownership-record-v1", "kinds": list(KINDS), "assertion_kinds": list(ASSERTION_KINDS)},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime", "DocumentStore", "LeiStore"],
                            "tools": ["ownership_provider_contracts", "acquire_ownership_sources",
                                      "record_ownership_register_document"]},
            "lookup": {"tools": ["lookup_ownership_entity", "inspect_ownership_record", "ownership_record_history"]},
            "identity": {"reuses": ["EntityHistoryStore", "canonical_entities", "market instrument master"],
                         "tools": ["propose_ownership_identity_matches", "review_ownership_identity_match",
                                   "revert_ownership_identity_match", "list_ownership_identity_candidates"]},
            "graph_and_timeline": {"tools": ["ownership_graph", "ownership_timeline", "ownership_state_as_of"]},
            "dossier": {"reuses": ["AuthoredReportStore"], "tools": ["build_ownership_dossier", "export_ownership_dossier"]},
            # Optional competition feature (#2217, default off): cases, stages and state aid naming a company.
            "competition": {"feature": "competition", "tools": ["lookup_competition_cases",
                                                                "competition_case_history",
                                                                "state_aid_awards_for_beneficiary",
                                                                "build_competition_dossier"]},
        },
    },
    "never": ["infer beneficial ownership", "make sanctions or AML determinations",
              "merge entities automatically", "scrape registers whose terms or access forbid it"],
}
_DDL = """
CREATE TABLE IF NOT EXISTS ownership_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


class BundleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _coordinator(conn):
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='composition_authority'").fetchone():
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator) -> bool:
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn, namespace: str) -> bool:
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ownership_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM ownership_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace: str) -> None:
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Corporate Ownership is disabled for this namespace; shared providers "
                                             "remain usable")


def set_enabled(conn, namespace: str, enabled: bool, *, principal_id: str, scopes: Iterable[str],
                now: Callable[[], int] | None = None) -> dict[str, Any]:
    if "operator" not in set(scopes):
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
    conn.execute("""INSERT INTO ownership_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET
                    enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms""",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "authority": "namespace-flag",
            "shared_capabilities_affected": []}


def _competition_on(conn) -> bool:
    """The optional ``competition`` feature (#2217, default off) gates its sources in a default acquisition."""
    from src.kb.competition import feature_enabled

    return feature_enabled(conn)


def load_source_pack() -> dict[str, Any]:
    from src.ingestion.source_packs import validate_source_pack

    return validate_source_pack(json.loads(SOURCE_PACK_PATH.read_text()))


def install_source_pack(conn, *, principal_id: str, scopes: Iterable[str], accept_terms: bool = False,
                        manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Install (operator) the corporate-ownership source pack and optionally record terms acceptance."""
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore

    if "operator" not in set(scopes):
        raise BundleError("unauthorized", "operator scope is required to install the source pack")
    value = dict(manifest or load_source_pack())
    store = SourcePackStore(conn)
    current = conn.execute("SELECT version FROM source_pack_current WHERE pack_id=?", [value["pack_id"]]).fetchone() \
        if conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='source_pack_current'").fetchone() else None
    if current is None:
        store.install(value, principal_id=principal_id, enable=True)
    if accept_terms:
        runtime = SourcePackRuntime(conn)
        for source in value["sources"]:
            runtime.accept_license(value["pack_id"], source["source_id"], principal_id=principal_id)
    return {"pack_id": value["pack_id"], "version": value["version"], "sources": [s["source_id"] for s in value["sources"]],
            "terms_accepted": accept_terms}


def acquire(conn, namespace: str, *, run_key: str, principal_id: str, scopes: Iterable[str],
            source_ids: Sequence[str] | None = None, adapters: Mapping[str, Any] | None = None,
            network: str = "fixture", secret_resolver: Callable[[str], str | None] | None = None,
            dns_resolver: Callable[[str], Sequence[str]] | None = None, now: Callable[[], int] | None = None,
            budgets: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Run bounded acquisition through the source-pack runtime, then project GLEIF Level 2 (O03-O06).

    ``network='live'`` compiles real adapters; otherwise ``adapters`` (fixture
    adapters) must be supplied. The runtime receipt is returned unchanged
    beside the GLEIF projection counts.
    """
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    if INGEST_SCOPE not in scopes and "operator" not in scopes:
        raise OwnershipError("unauthorized", f"{INGEST_SCOPE} is required to run acquisition")
    require_enabled(conn, namespace)
    runtime = SourcePackRuntime(conn, **({"now": now} if now else {}), sleep=lambda _s: None)
    manifest, _ = runtime._manifest(SOURCE_PACK_ID)
    selected = [s for s in manifest["sources"] if (s["source_id"] in source_ids if source_ids else
                                                   s["connector"] != "competition" or _competition_on(conn))]
    if any(dict(s.get("ownership") or {}).get("namespace", namespace) != namespace for s in selected):
        raise OwnershipError("invalid_request", "the installed source pack projects into a different namespace")
    receipts = {}
    limits = {"max_results": 5000, "max_bytes": 50_000_000, "max_pages": 100, "timeout_ms": 120_000, **dict(budgets or {})}
    for operation in sorted({op for s in selected for op in s["operations"]}):
        ids = [s["source_id"] for s in selected if operation in s["operations"]]
        request = {"pack_id": SOURCE_PACK_ID, "run_key": f"{run_key}:{operation}", "operation": operation,
                   "source_ids": ids, **limits}
        if network == "live":
            request["network"] = "live"
        receipts[operation] = runtime.run(request, principal_id=principal_id, adapters=adapters,
                                          secret_resolver=secret_resolver, dns_resolver=dns_resolver)
    projection = None
    gleif = [s for s in selected if s["connector"] == "gleif"]
    for source in gleif:
        lei = dict(source.get("lei") or {})
        projection = OwnershipStore(conn, **({"now": now} if now else {})).project_gleif(
            namespace, lei.get("leis") or [], lei_namespace=str(lei.get("namespace") or "global"),
            run_id=(receipts.get("entities") or {}).get("run_id", run_key), principal_id=principal_id)
    return {"receipts": receipts, "gleif_projection": projection,
            "statuses": {op: r.get("status") for op, r in receipts.items()}}


def readiness(conn, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
    scopes = set(scopes)
    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "ownership read scope is required")
    has_records = bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ownership_records'").fetchone())
    counts = {}
    if has_records:
        counts = dict(conn.execute("SELECT provider, count(*) FROM ownership_records WHERE namespace=? GROUP BY provider",
                                   [namespace]).fetchall())
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] == "not-implemented":
            status = "not-implemented"
        elif counts.get(provider):
            status = "fixture-or-live-records-present"
        else:
            status = "no-records"
        providers[provider] = {"status": status, "records": counts.get(provider, 0),
                               "live_verification": LIVE_VERIFICATION[provider]}
    result = {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
              "providers": providers,
              "entry_points": {"lookup": "ready" if counts else "unavailable",
                               "graph_and_timeline": "ready" if counts else "unavailable",
                               "dossier": "ready" if counts else "unavailable"},
              "composition_runtime": BUNDLE["architecture"]["status"]}
    coordinator = _coordinator(conn)
    if coordinator is not None and _selected(coordinator):
        assessment = coordinator.readiness(scopes=scopes)
        plan = coordinator.active()["plan"]
        bound = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
        assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in bound]
        result["composition"] = assessment
    return result


def dossier_with_competition(conn, namespace: str, scheme: str, value: str, *, competition_namespace: str,
                             principal_id: str | None, scopes: Iterable[str], as_of: str | None = None,
                             group: bool = False, evidence_kind: str = "unspecified") -> dict[str, Any]:
    """The ownership dossier plus a ``competition`` section (#2217): the cases and aid awards naming the company.

    The section comes from the optional ``competition`` feature's queries over reviewed identity matches only; it
    never predicts outcomes, assesses market power or aid compatibility, or gives legal advice.
    """
    from src.kb.competition_queries import awards_for_beneficiary, cases_for_company
    from src.kb.ownership_dossier import build_dossier

    dossier = build_dossier(conn, namespace, scheme, value, principal_id=principal_id, scopes=scopes, as_of=as_of,
                            evidence_kind=evidence_kind)
    if dossier.get("status") != "assembled":
        return dossier
    root = dossier["identity"]["root"]
    cases = cases_for_company(conn, competition_namespace, root, ownership_namespace=namespace, scopes=scopes,
                              as_of=as_of, group=group, principal_id=principal_id)
    awards = awards_for_beneficiary(conn, competition_namespace, root, ownership_namespace=namespace, scopes=scopes,
                                    principal_id=principal_id)
    return {**dossier, "competition": {"namespace": competition_namespace, "status": cases["status"],
                                       "cases": cases["cases"], "by_authority": cases["by_authority"],
                                       "group_members": cases["group_members"], "awards": awards["awards"],
                                       "award_totals": awards["computed_totals"], "unknowns": awards["unknowns"],
                                       "coverage": cases["coverage"], "notice": cases["notice"]}}
