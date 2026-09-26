"""Built-in registered bindings and readiness probes.

Probes observe; they never issue provider requests, acquire data or change
schedules. Each returns ``{"state": ..., "blockers": [...]}`` where blocker
kinds come from the closed set in :data:`src.composition.contracts.BLOCKER_KINDS`
and never carry record content or credential values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.composition.bindings import register_probe


def _table_rows(conn: Any, table: str, where: str = "") -> int | None:
    if conn is None:
        return None
    try:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
        if not exists:
            return None
        return int(conn.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0])
    except Exception:  # noqa: BLE001 - probes are observations, never failures
        return None


def _ready() -> dict[str, Any]:
    return {"state": "ready", "blockers": []}


def _blocked(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"state": "blocked", "blockers": [{"kind": kind, "reason": reason, **extra}]}


@register_probe("geospatial.local-store", "Local spatial stores are configured and hold data")
def geospatial_local_store(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    features = _table_rows(conn, "geospatial_feature_current", "WHERE lifecycle='active'")
    geometries = _table_rows(conn, "geospatial_geometries")
    if features is None and geometries is None:
        return _blocked("provider-unavailable", "the local spatial store is not configured")
    if not (features or 0) and not (geometries or 0):
        return _blocked("empty-data", "the local spatial store holds no features or geometries")
    return _ready()


@register_probe("geospatial.live-source", "The Berlin WFS source pack is ready for live acquisition")
def geospatial_live_source(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    if conn is None:
        return _blocked("provider-unavailable", "no warehouse connection is configured")
    try:
        from src.kb.geospatial_features import pack_readiness

        readiness = pack_readiness(conn)
    except Exception:  # noqa: BLE001 - probes are observations
        return _blocked("provider-unavailable", "source-pack readiness could not be observed")
    codes = {blocker.get("code") for blocker in readiness.get("blockers", [])}
    if "pack_not_installed" in codes or "pack_disabled" in codes:
        return _blocked("provider-disabled", "the source pack is not installed or is disabled")
    if readiness.get("modes", {}).get("live") != "ready":
        if "license_not_accepted" in codes:
            return _blocked("unverified-live", "source terms have not been accepted for live use")
        return _blocked("unverified-live", "live acquisition has not been verified")
    if context.get("network") != "live":
        return _blocked("unverified-live", "network access is disabled for this caller")
    return _ready()


# --------------------------------------------------------------------------- #
# Registered bindings for the first composition (C07.5). These are real code
# paths against local data: the source-pack runtime, the geospatial stores, the
# document revision store and the intake session ledger. Fixture data enters
# only as provider input (pages served by the runtime's fixture adapter or an
# injected transport), never as a stand-in for tool execution.
# --------------------------------------------------------------------------- #

import json as _json  # noqa: E402

from src.composition.bindings import register_binding, register_gate  # noqa: E402
from src.composition.workflows import record_reference  # noqa: E402


@register_gate("osint.gated-tools")
def osint_gated_tools(ctx: Any) -> bool:
    """The OSINT review gate (``NOESIS_OSINT_GATED_TOOLS``), whichever pack asks."""

    from src.agent.runtime import gated_tools_enabled

    return gated_tools_enabled()


@register_probe("catalog.required-data", "Legacy catalog data readiness for the declared required data")
def catalog_required_data(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """Reuse the catalog's own data-state evaluation for migrated bundles."""

    from src.mcp_host.catalog import _data_state

    state, _reason = _data_state(list(context.get("required_data") or []), conn)
    if state == "available":
        return _ready()
    if state == "empty":
        return _blocked("empty-data", "required data is empty")
    if state == "degraded":
        return _blocked("inaccessible-data", "data readiness could not be probed")
    return _blocked("provider-unavailable", "the store behind the required data is not configured")


@register_probe("sources.runtime", "The source-pack runtime is configured")
def sources_runtime(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    if _table_rows(conn, "source_pack_current") is None:
        return _blocked("provider-unavailable", "the source-pack runtime is not configured")
    return _ready()


@register_probe("documents.store", "The document revision store holds committed revisions")
def documents_store(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    rows = _table_rows(conn, "document_revision_records")
    if rows is None:
        return _blocked("provider-unavailable", "the document revision store is not configured")
    if not rows:
        return _blocked("empty-data", "no document revisions are committed")
    return _ready()


@register_probe("intake.store", "The intake session ledger is configured")
def intake_store(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    if _table_rows(conn, "intake_sessions") is None:
        return _blocked("provider-unavailable", "the intake session ledger is not configured")
    return _ready()


@register_probe("osint.claims", "The OSINT claim layer holds claims")
def osint_claims(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    rows = _table_rows(conn, "argument_claims")
    if rows is None:
        return _blocked("provider-unavailable", "no claim layer is available")
    if not rows:
        return _blocked("empty-data", "the claim layer holds no claims")
    return _ready()


@register_binding(
    "sources.acquire-shared",
    "Acquire through the source-pack runtime, sharing equivalent acquisitions",
    arguments_schema={"type": "object", "required": ["pack_id"], "properties": {
        "pack_id": {"type": "string"}, "operation": {"type": "string"},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "parameters": {"type": "object"}, "max_results": {"type": "integer"},
        "max_bytes": {"type": "integer"}, "max_pages": {"type": "integer"},
        "version": {"type": "string"}}},
    result_contract="noesis-source-pack-run-receipt-v1",
    result_path="receipt",
)
def sources_acquire_shared(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    ctx.require_effect("acquisition")
    runtime = SourcePackRuntime(ctx.conn)
    pack_id = arguments["pack_id"]
    status = ctx.conn.execute(
        "SELECT version FROM source_pack_current WHERE pack_id=?", [pack_id]).fetchone()
    if status is None:
        from src.ingestion.source_packs import SourcePackError

        raise SourcePackError("not_found", "source pack is not installed")
    if arguments.get("version") and arguments["version"] != status[0]:
        from src.kb.research_recipes import RecipeError

        raise RecipeError("source_pin_mismatch",
                          f"template pins {pack_id}@{arguments['version']}, installed {status[0]}")
    request = {"pack_id": pack_id, "operation": arguments.get("operation", "features"),
               "source_ids": list(arguments.get("source_ids", [])),
               "parameters": dict(arguments.get("parameters", {})),
               "network": "live" if ctx.network == "live" else "disabled"}
    for key in ("max_results", "max_bytes", "max_pages"):
        if key in arguments:
            request[key] = arguments[key]
    supplied = dict(ctx.inputs.get("source_adapters", {}).get(pack_id, {}))
    fixture_root = ctx.inputs.get("fixture_root")
    if not supplied and fixture_root is not None:
        supplied = runtime.fixture_adapters(pack_id, fixture_root)
        provider_input = "fixture-pages"
    else:
        provider_input = "injected-transport" if supplied else "live"
    if provider_input == "live" and ctx.network != "live":
        from src.ingestion.source_packs import SourcePackError

        raise SourcePackError("network_policy", "live acquisition needs network access")
    if request["source_ids"]:
        supplied = {k: v for k, v in supplied.items() if k in request["source_ids"]}
    receipt = runtime.run_shared(
        request, consumer=ctx.consumer or ctx.principal_id, namespace=ctx.namespace,
        access_context="public", principal_id=ctx.principal_id, adapters=supplied,
        dns_resolver=ctx.inputs.get("dns_resolver"))
    shared = receipt.pop("shared", {})
    return {"contract": "noesis-composition-acquisition-v1", "receipt": receipt, "shared": shared,
            "provider_input": provider_input,
            "references": [record_reference("sources.acquire", "source_pack_run", receipt["run_id"],
                                            ctx.namespace, receipt["receipt_hash"])]}


@register_binding(
    "geospatial.features-within",
    "Points of a collection inside one boundary, with evidence (owner query receipt)",
    arguments_schema={"type": "object", "required": ["collection"], "properties": {
        "collection": {"type": "string"}, "boundary_name": {"type": ["string", "null"]},
        "boundary_feature_id": {"type": ["string", "null"]},
        "boundary_collection": {"type": ["string", "null"]}, "limit": {"type": "integer"}}},
    result_contract="noesis-geospatial-feature-query-v1",
)
def geospatial_features_within(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from src.kb.geospatial_features import GeospatialFeatureStore

    result = GeospatialFeatureStore(ctx.conn, initialize=False).within(
        ctx.namespace, collection=arguments["collection"],
        boundary_name=arguments.get("boundary_name"),
        boundary_feature_id=arguments.get("boundary_feature_id"),
        boundary_collection=arguments.get("boundary_collection"),
        principal_id=ctx.principal_id, scopes=set(ctx.scopes), limit=int(arguments.get("limit", 1000)))
    return dict(result)


def _license(conn: Any, pack_id: str | None, manifest_hash: str | None, source_id: str | None):
    if not pack_id:
        return "internal", False
    row = conn.execute(
        "SELECT manifest_json FROM source_pack_versions WHERE pack_id=? AND manifest_hash=?",
        [pack_id, manifest_hash]).fetchone()
    if row is None:
        return "internal", False
    manifest = _json.loads(row[0])
    for source in manifest.get("sources", []):
        if source_id in (None, source["source_id"]):
            terms = str(source.get("license", {}).get("redistribution", ""))
            if terms.startswith("permitted"):
                return "public", True
    return "internal", False


@register_binding(
    "evidence.document-references",
    "Pinned references to the committed document revisions behind records",
    arguments_schema={"type": "object", "required": ["document_ids"], "properties": {
        "document_ids": {"type": "array", "items": {"type": "string"}}}},
    result_contract="noesis-composition-evidence-references-v1",
)
def evidence_document_references(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    references = []
    missing = []
    for document_id in sorted(set(arguments["document_ids"])):
        row = ctx.conn.execute(
            "SELECT revision, payload_json, source_id FROM document_revision_records "
            "WHERE document_id=? AND committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1",
            [document_id]).fetchone()
        if row is None:
            missing.append(document_id)
            continue
        metadata = _json.loads(row[1]).get("metadata") or {}
        classification, redistribution = _license(
            ctx.conn, metadata.get("source_pack_id"), metadata.get("source_pack_manifest_hash"), row[2])
        references.append({**record_reference("documents", "document", document_id, ctx.namespace,
                                              int(row[0])),
                           "classification": classification, "redistribution": redistribution})
    return {"contract": "noesis-composition-evidence-references-v1", "references": references,
            "unresolved": missing}


def _intake_lookup(ctx: Any, key: str) -> dict[str, Any] | None:
    row = ctx.conn.execute(
        "SELECT revision FROM intake_session_commands WHERE session_id=? AND command_key=?",
        [ctx.session_id, f"composition:{key[:48]}"]).fetchone()
    if row is None:
        return None
    return {"contract": "noesis-composition-session-artifact-v1", "session_id": ctx.session_id,
            "revision": int(row[0]), "adopted": True, "references": []}


@register_binding(
    "intake.session-artifact",
    "Record a workflow artifact and its evidence references on the intake session",
    arguments_schema={"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "content": {}, "coverage": {"type": "object"},
        "references": {"type": "array"}}},
    lookup=_intake_lookup,
    result_contract="noesis-composition-session-artifact-v1",
)
def intake_session_artifact(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from src.kb.intake_modes import IntakeStore

    ctx.require_effect("local-mutation")
    if not ctx.session_id:
        from src.kb.research_recipes import RecipeError

        raise RecipeError("no_session", "session artifacts need a bound intake session")
    store = IntakeStore(ctx.conn, initialize=False)
    current = store.inspect(ctx.namespace, ctx.session_id, principal_id=ctx.principal_id,
                            scopes=set(ctx.scopes))
    coverage = dict(arguments.get("coverage") or {})
    if "sources" in coverage:
        acquired = coverage.pop("acquired", None)
        sources = sorted(coverage.pop("sources"))
        coverage = {**coverage, "sources": sources,
                    "acquisition": "complete" if acquired else "unavailable",
                    "missing_sources": [] if acquired else sources}
    refs = []
    for ref in arguments.get("references") or []:
        if isinstance(ref, Mapping) and ref.get("record_kind") == "document":
            refs.append({"kind": "document", "id": ref["record_id"], "namespace": ref["namespace"],
                         "version": int(ref["revision"])})
    state = store.command(
        ctx.namespace, ctx.session_id, f"composition:{ctx.idempotency_key[:48]}",
        expected_revision=current["revision"], action="record",
        payload={"data": {f"artifact:{arguments['name']}": {
            "content": arguments.get("content"), "coverage": coverage,
            "run_id": ctx.run_id, "step_id": ctx.step_id}}, "references": refs},
        principal_id=ctx.principal_id, scopes=set(ctx.scopes))
    return {"contract": "noesis-composition-session-artifact-v1", "session_id": ctx.session_id,
            "revision": int(state["revision"]), "adopted": False, "coverage": coverage,
            "references": [dict(r) for r in arguments.get("references") or [] if isinstance(r, Mapping)]}


@register_binding(
    "research.place-literature",
    "Committed documents that mention a place, as pinned references",
    arguments_schema={"type": "object", "required": ["place"], "properties": {
        "place": {"type": "string", "minLength": 1}, "limit": {"type": "integer"}}},
    result_contract="noesis-composition-literature-v1",
)
def research_place_literature(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    needle = f"%{arguments['place'].lower()}%"
    rows = ctx.conn.execute(
        "SELECT r.document_id, max(r.revision) FROM document_revision_records r "
        "WHERE r.committed_watermark IS NOT NULL AND (lower(r.payload_json) LIKE ?) "
        "GROUP BY r.document_id ORDER BY r.document_id LIMIT ?",
        [needle, int(arguments.get("limit", 50))]).fetchall()
    return {"contract": "noesis-composition-literature-v1", "place": arguments["place"],
            "references": [record_reference("documents", "document", row[0], ctx.namespace, int(row[1]))
                           for row in rows]}


@register_binding(
    "osint.event-geolocation",
    "Event geography from claim text; review-gated and never locates a person",
    arguments_schema={"type": "object", "properties": {
        "topic": {"type": ["string", "null"]}, "limit": {"type": "integer"}}},
    gate="osint.gated-tools",
    result_contract="noesis-composition-event-geolocation-v1",
)
def osint_event_geolocation(ctx: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from src.osint.gated import geolocate_claims

    result = geolocate_claims(ctx.conn, topic=arguments.get("topic"), limit=int(arguments.get("limit", 40)))
    return {"contract": "noesis-composition-event-geolocation-v1", **dict(result), "references": []}
