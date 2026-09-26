"""Real local execution adapters for the first composition (C07.5).

Each handler calls the owning subsystem's real code path against local data
and returns that owner's receipt. What may be *fixture* here is provider
input only (for example page fixtures fed to the source-pack runtime); tool
execution is never mocked. Receipts label the input kind so fixture input
cannot be mistaken for live acquisition.

* ``acquire_source`` — :class:`SourcePackRuntime` run (acquisition/projection);
* ``spatial_relation`` — :class:`GeospatialStore.relation` (read-only query);
* ``session_artifact`` — :class:`IntakeStore.command` ``record`` (local mutation).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.composition.workflows import StepContext

SOURCE_TOOL = "noesis-knowledge-engine.run_source_pack_execution"
SPATIAL_TOOL = "noesis-knowledge-engine.calculate_spatial_relation"
SESSION_TOOL = "noesis-knowledge-engine.command_intake_mode"
FEATURES_TOOL = "noesis-knowledge-engine.query_geospatial_features_within"
CORROBORATE_TOOL = "noesis-osint.corroborate"
LITERATURE_TOOL = "noesis-research.literature_claims"


def acquire_source(runtime: Any, *, principal_id: str, fixture_adapters: Mapping[str, Any] | None = None,
                   dns_resolver: Callable[[str], Any] | None = None) -> Callable[[Mapping[str, Any], StepContext], dict]:
    """Source-pack acquisition. ``fixture_adapters`` supply provider *input* only."""

    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("acquisition")
        request = {**dict(arguments["request"]), "run_key": f"{arguments['request']['run_key']}:{context.idempotency_key[:16]}"}
        receipt = runtime.run(request, principal_id=principal_id, adapters=fixture_adapters,
                              dns_resolver=dns_resolver or (lambda _host: ["8.8.8.8"]))
        return {"receipt": {"owner": "source-pack-runtime", "run_id": receipt["replay"]["run_id"],
                            "receipt_hash": receipt["receipt_hash"]},
                "status": receipt["status"],
                "documents": sum(s["counts"]["inserted"] for s in receipt["sources"]),
                "provider_input": "fixture" if fixture_adapters else "live",
                "tool_execution": "real"}

    return handler


def source_run_receipt(conn: Any, pack_id: str) -> Callable[[str], Mapping[str, Any] | None]:
    """Owner receipt lookup for reconciliation: the runtime's run keyed by the dispatcher key."""

    def lookup(idempotency_key: str) -> Mapping[str, Any] | None:
        row = conn.execute("SELECT run_id, receipt_json FROM source_pack_runs WHERE pack_id=? AND run_key LIKE ? "
                           "AND status IN ('complete','partial')", [pack_id, f"%:{idempotency_key[:16]}"]).fetchone()
        if not row:
            return None
        import json

        receipt = json.loads(row[1])
        return {"result": {"receipt": {"owner": "source-pack-runtime", "run_id": row[0],
                                       "receipt_hash": receipt["receipt_hash"]},
                           "status": receipt["status"], "provider_input": "fixture", "tool_execution": "real",
                           "documents": sum(s["counts"]["inserted"] for s in receipt["sources"])}}

    return lookup


def spatial_relation(store: Any, *, principal_id: str, scopes: set[str]) -> Callable[[Mapping[str, Any], StepContext], dict]:
    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("read-only")
        result = store.relation(arguments["namespace"], arguments["operation"], arguments["left_geometry_id"],
                                arguments["right"], scopes=scopes, principal_id=principal_id,
                                tolerance_m=float(arguments.get("tolerance_m", 0)))
        evidence = [{"kind": "geometry", "id": arguments["left_geometry_id"],
                     "namespace": arguments["namespace"], "restrictions": list(arguments.get("restrictions") or [])}]
        return {"relation": result, "receipt": {"owner": "geospatial",
                                                "receipt_id": (result.get("receipt") or {}).get("receipt_id")},
                "evidence": evidence, "tool_execution": "real"}

    return handler


def session_artifact(intake: Any, *, principal_id: str, scopes: set[str]) -> Callable[[Mapping[str, Any], StepContext], dict]:
    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("local-mutation")
        state = intake.inspect(arguments["namespace"], arguments["session_id"], principal_id=principal_id,
                               scopes=scopes)
        result = intake.command(arguments["namespace"], arguments["session_id"], f"artifact:{context.idempotency_key[:32]}",
                                expected_revision=state["revision"], action="record",
                                payload={"data": {"artifacts": {arguments["artifact_id"]: dict(arguments["artifact"])}}},
                                principal_id=principal_id, scopes=scopes)
        return {"receipt": {"owner": "intake-session", "session_id": arguments["session_id"],
                            "revision": result["revision"]},
                "artifact_id": arguments["artifact_id"], "tool_execution": "real"}

    return handler


def features_within(store: Any, *, principal_id: str, scopes: set[str],
                    reference_limit: int = 25) -> Callable[[Mapping[str, Any], StepContext], dict]:
    """Points of a collection inside a named boundary, with cross-pack record references.

    Each reference names the owner capability, native record kind and id,
    namespace and revision, so two packs referencing the same place get the
    same identity.
    """

    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("read-only")
        result = store.within(arguments["namespace"], collection=arguments["collection"],
                              boundary_name=arguments["boundary_name"],
                              boundary_collection=arguments["boundary_collection"], principal_id=principal_id,
                              scopes=scopes, limit=int(arguments.get("limit", 1000)))
        members = result.get("members") or []
        references = [{"owner_capability": "geospatial.feature-query", "record_kind": "feature",
                       "native_id": m["native_id"], "feature_id": m["feature_id"],
                       "namespace": m.get("namespace", arguments["namespace"]), "revision_id": m["revision_id"]}
                      for m in members[:reference_limit]]
        boundary = result.get("boundary") or {}
        return {"status": result.get("status"), "total_members": result.get("total_members", len(members)),
                "boundary": {"feature_id": boundary.get("feature_id"), "revision_id": boundary.get("revision_id")},
                "references": references, "evidence": [{**ref, "restrictions": []} for ref in references],
                "receipt": {"owner": "geospatial-features",
                            "receipt_id": (result.get("receipt") or {}).get("receipt_id")},
                "tool_execution": "real"}

    return handler


def corroboration(conn: Any) -> Callable[[Mapping[str, Any], StepContext], dict]:
    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("read-only")
        from src.osint import corroborate

        return {"corroboration": corroborate(conn, str(arguments["claim_id"])), "tool_execution": "real"}

    return handler


def literature(conn: Any) -> Callable[[Mapping[str, Any], StepContext], dict]:
    def handler(arguments: Mapping[str, Any], context: StepContext) -> dict[str, Any]:
        context.record_effect("read-only")
        from src.domains.research.analytics import literature_claims

        return {"literature": literature_claims(conn, arguments.get("topic")), "tool_execution": "real"}

    return handler


def session_command_receipt(conn: Any) -> Callable[[str], Mapping[str, Any] | None]:
    """Owner receipt for an artifact command, keyed like :func:`session_artifact`."""

    def lookup(idempotency_key: str) -> Mapping[str, Any] | None:
        row = conn.execute("SELECT session_id, revision FROM intake_session_commands WHERE command_key=?",
                           [f"artifact:{idempotency_key[:32]}"]).fetchone()
        if not row:
            return None
        return {"result": {"receipt": {"owner": "intake-session", "session_id": row[0], "revision": row[1]},
                           "tool_execution": "real", "reconciled": True}}

    return lookup
