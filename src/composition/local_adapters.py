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
