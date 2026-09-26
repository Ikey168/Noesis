"""Plan-driven catalog view, operation readiness and explanations (C04).

* :class:`CompositionView` maps catalog tool IDs to the plan binding that
  owns them (C04.1). Tools with no binding keep the legacy catalog tables,
  so nothing changes for bundles that are not composition-managed.
* :func:`assess` produces a ``noesis-composition-readiness-v1`` document per
  bound operation (C04.2). It does not add a state machine: each operation's
  blockers are fed through the catalog's own ``_state`` priority.
* :meth:`CompositionView.explain` answers what provides a capability, who
  consumes it, why it was selected, what is pinned and why a workflow is
  blocked (C04.3).
* Everything emitted is redacted to the caller (C04.5): consumers and
  selections outside the caller's visibility are omitted, inaccessible data
  is named only as inaccessible, and credential values never appear.

The assessment is observation-time state: it records ``observed_at_ms`` and
the plan digest it assessed, and is never written back into a plan.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.composition.contracts import READINESS_CONTRACT, SECRET_KEYS

REDACTED = "[redacted]"
_CATALOG_TO_READINESS = {"available": "ready", "degraded": "degraded"}


# ------------------------------------------------------------ probes


def _table_exists(conn: Any, table: str) -> bool:
    if conn is None:
        return False
    try:
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())
    except Exception:  # noqa: BLE001 - readiness probes are best effort
        return False


def probe_state(probes: Sequence[Mapping[str, Any]], conn: Any, *,
                enabled_source_packs: Iterable[str] | None = None) -> tuple[str, str | None]:
    """Evaluate descriptor probes in the catalog's data-state vocabulary.

    ``table-exists``: a missing table is ``unavailable``. ``table-rows``: a
    missing table is ``unavailable``, zero rows is ``empty``. ``source-pack-
    enabled``: a disabled source pack is ``unavailable``. ``catalog-tool-state``
    defers to the catalog's own tool state and adds nothing here.
    """

    enabled = set(enabled_source_packs) if enabled_source_packs is not None else None
    states: list[tuple[str, str]] = []
    for probe in sorted(probes, key=lambda p: str(p.get("id"))):
        kind, target, probe_id = probe.get("kind"), str(probe.get("target")), str(probe.get("id"))
        if kind in {"table-exists", "table-rows"}:
            if not _table_exists(conn, target):
                states.append(("unavailable", f"required data {probe_id!r} is not accessible"))
                continue
            if kind == "table-rows":
                try:
                    count = int(conn.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0])
                except Exception:  # noqa: BLE001 - readiness probes are best effort
                    states.append(("degraded", f"required data {probe_id!r} could not be probed"))
                    continue
                if not count:
                    states.append(("empty", f"required data {probe_id!r} has no retained records"))
        elif kind == "source-pack-enabled" and enabled is not None and target not in enabled:
            states.append(("unavailable", f"source pack for {probe_id!r} is not enabled"))
    for state in ("unavailable", "degraded", "empty"):
        for found, reason in states:
            if found == state:
                return state, reason
    return "available", None


# ------------------------------------------------------------ view


@dataclass(frozen=True)
class ToolBinding:
    tool: str
    capability: str
    provider: str
    provider_version: str
    operation: str
    pack: str
    reason: str
    consumers: tuple[str, ...]
    probes: tuple[Mapping[str, Any], ...]
    required_scopes: tuple[str, ...]
    required_context: tuple[str, ...]

    @property
    def required_data(self) -> list[str]:
        return sorted({str(p["id"]) for p in self.probes})

    def as_catalog_field(self) -> dict[str, Any]:
        return {"capability": self.capability, "provider": self.provider,
                "provider_version": self.provider_version, "operation": self.operation, "pack": self.pack,
                "reason": self.reason}


class CompositionView:
    """The catalog's window onto one resolved plan and its pinned descriptors."""

    def __init__(self, plan: Mapping[str, Any], descriptors: Iterable[Mapping[str, Any]],
                 manifests: Iterable[Mapping[str, Any]] = ()) -> None:
        self.plan = plan
        pins = {(p["id"], p["version"]) for p in plan.get("providers") or []}
        self.descriptors = {d["id"]: d for d in descriptors if (d["id"], d["version"]) in pins}
        pinned_packs = {(p["id"], p["version"]) for p in plan.get("packs") or []}
        self.manifests = {m["id"]: m for m in manifests if (m["id"], m["version"]) in pinned_packs}
        contributors = {e["to"]: e["from"] for e in plan.get("graph") or [] if e.get("kind") == "contributes"}
        self.tools: dict[str, ToolBinding] = {}
        self.bindings = {b["capability"]: b for b in plan.get("bindings") or []}
        for capability, binding in sorted(self.bindings.items()):
            descriptor = self.descriptors.get(binding["provider"])
            if descriptor is None:
                continue
            probes = {p["id"]: p for p in descriptor.get("readiness_probes") or []}
            operations = {op["id"]: op for op in descriptor.get("operations") or []}
            for op_id in binding["operations"]:
                op = operations[op_id]
                self.tools[op["tool"]] = ToolBinding(
                    tool=op["tool"], capability=capability, provider=binding["provider"],
                    provider_version=binding["provider_version"], operation=op_id,
                    pack=contributors.get(capability) or binding["provider"].split(".")[0],
                    reason=binding["reason"], consumers=tuple(binding["consumers"]),
                    probes=tuple(p for p in [probes.get(op.get("readiness_probe"))] if p),
                    required_scopes=tuple(op.get("required_scopes") or ()),
                    required_context=tuple(op.get("required_context") or ()))

    def for_tool(self, tool_id: str) -> ToolBinding | None:
        return self.tools.get(tool_id)

    # ------------------------------------------------------------ C04.3

    def explain(self, readiness: Mapping[str, Any] | None = None, *, visible_packs: Iterable[str] | None = None,
                principal: str | None = None, selections: Mapping[str, Iterable[str]] | None = None,
                ) -> dict[str, Any]:
        """Capability and workflow explanations, redacted to the caller.

        ``visible_packs``: packs the caller may see (others are omitted from
        consumer lists and workflows, not masked); ``selections``: workflow
        selections keyed by principal, of which only ``principal``'s appear.
        """

        visible = set(visible_packs) if visible_packs is not None else None
        states = {(o["capability"], o["operation"]): o for o in (readiness or {}).get("operations") or []}
        pins = {p["id"]: p for p in self.plan.get("providers") or []}
        capabilities = []
        for capability, binding in sorted(self.bindings.items()):
            consumers = [c for c in binding["consumers"] if visible is None or c in visible]
            if visible is not None and not consumers:
                continue
            ops = [op for op in self.tools.values() if op.capability == capability]
            capabilities.append({
                "capability": capability,
                "contract": dict(binding["contract"]),
                "provider": binding["provider"],
                "pinned": {"version": binding["provider_version"],
                           "content_hash": pins.get(binding["provider"], {}).get("content_hash")},
                "selected_because": {"explicit": "configured explicitly or declared by the contributing pack",
                                     "only-compatible": "the only compatible provider in the candidate set"}[
                                         binding["reason"]],
                "reason": binding["reason"],
                "consumers": consumers,
                "operations": [{"operation": op.operation, "tool": op.tool, "required_data": op.required_data,
                                **_state_of(states.get((capability, op.operation)))}
                               for op in sorted(ops, key=lambda o: o.operation)],
            })
        workflows = []
        for pack_id, manifest in sorted(self.manifests.items()):
            if visible is not None and pack_id not in visible:
                continue
            needed = [(c, op.operation) for c, b in sorted(self.bindings.items()) if pack_id in b["consumers"]
                      for op in sorted(self.tools.values(), key=lambda o: o.operation) if op.capability == c]
            for template in (manifest.get("contributes") or {}).get("workflow_templates") or []:
                workflows.append(_workflow(template, pack_id, needed, states, self.plan))
        payload = {"plan_digest": self.plan["digest"], "capabilities": capabilities, "workflows": workflows}
        if principal is not None and selections:
            payload["selected_workflows"] = sorted(selections.get(principal) or [])
        return payload


def _state_of(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"state": "unassessed", "blockers": []}
    return {"state": entry["state"], "blockers": [dict(b) for b in entry["blockers"]]}


def _workflow(template: Mapping[str, Any], pack_id: str, needed: Sequence[tuple[str, str]],
              states: Mapping[tuple[str, str], Mapping[str, Any]], plan: Mapping[str, Any]) -> dict[str, Any]:
    blocking, degraded = [], []
    for capability, operation in needed:
        entry = states.get((capability, operation))
        if entry is None:
            continue
        for blocker in entry["blockers"]:
            record = {"capability": capability, "operation": operation, "kind": blocker["kind"],
                      "detail": blocker.get("detail", "")}
            (blocking if entry["state"] == "blocked" else degraded).append(record)
    state = "blocked" if blocking else "degraded" if degraded else "ready" if states else "unassessed"
    omissions = [dict(o) for o in plan.get("omissions") or [] if o["pack"] == pack_id and o["reason"] != "not selected"]
    return {"workflow": template["id"], "version": template["version"], "pack": pack_id, "state": state,
            "operations": [{"capability": c, "operation": o} for c, o in needed],
            "blocked_by": blocking, "degraded_by": degraded, "optional_omissions": omissions}


# ------------------------------------------------------------ C04.2 readiness


def assess(view: CompositionView, *, conn: Any = None, namespace: str = "global", principal_ref: str | None = None,
           scopes: Iterable[str] = (), accessible_namespaces: Iterable[str] | None = None,
           credentials: Mapping[str, Any] | None = None, disabled_providers: Iterable[str] = (),
           enabled_packs: Iterable[str] | None = None, live_verified: Iterable[str] = (),
           failures: Mapping[str, str] | None = None, enabled_source_packs: Iterable[str] | None = None,
           shutdown_providers: Mapping[str, str] | None = None,
           exhausted_providers: Iterable[str] = (),
           now_ms: Callable[[], int] | None = None) -> dict[str, Any]:
    """Operation-specific readiness under the caller's namespace and grants.

    Blocker kinds stay distinct (``contracts.BLOCKER_KINDS``). ``credentials``
    maps provider IDs to configured credentials; only presence is read, the
    value is never emitted. ``live_verified``: ``provider.operation`` keys
    whose network access has been verified; operations needing the network
    without it are ``unverified_live_access`` (offline or unchecked).
    ``failures``: ``provider.operation`` → last failed-execution summary.
    """

    from src.mcp_host.catalog import _state

    granted = set(scopes)
    accessible = set(accessible_namespaces) if accessible_namespaces is not None else None
    credentials = credentials or {}
    disabled = set(disabled_providers)
    packs = set(enabled_packs) if enabled_packs is not None else None
    verified = set(live_verified)
    failures = failures or {}
    operations = []
    for tool in sorted(view.tools.values(), key=lambda t: (t.capability, t.operation)):
        key = f"{tool.provider}.{tool.operation}"
        blockers: list[dict[str, str]] = []
        missing_scopes = sorted(set(tool.required_scopes) - granted)
        if missing_scopes:
            blockers.append({"kind": "unauthorized", "detail": f"missing scopes {missing_scopes}"})
        if tool.provider in disabled or (packs is not None and tool.pack not in packs):
            blockers.append({"kind": "disabled_provider", "detail": f"provider {tool.provider} is not enabled"})
        elif tool.provider in (shutdown_providers or {}):
            blockers.append({"kind": "disabled_provider",
                             "detail": f"provider {tool.provider} was administratively shut down"})
        data_state, data_reason = "available", None
        if accessible is not None and namespace not in accessible:
            blockers.append({"kind": "inaccessible_data", "detail": "required data is not accessible to this caller"})
        else:
            data_state, data_reason = probe_state(tool.probes, conn, enabled_source_packs=enabled_source_packs)
            if data_state == "unavailable":
                blockers.append({"kind": "inaccessible_data", "detail": data_reason or ""})
            elif data_state == "empty":
                blockers.append({"kind": "empty_data", "detail": data_reason or ""})
        if "credentials" in tool.required_context and not credentials.get(tool.provider):
            blockers.append({"kind": "missing_credentials",
                             "detail": f"provider {tool.provider} needs credentials that are not configured"})
        needs_network = "network" in tool.required_context
        if needs_network and key not in verified:
            blockers.append({"kind": "unverified_live_access",
                             "detail": f"live access for {tool.operation} is offline or not verified"})
        if tool.provider in set(exhausted_providers) and "network" in tool.required_context:
            blockers.append({"kind": "aggregate_limit_exhausted",
                             "detail": f"the shared account limit for {tool.provider} is exhausted"})
        if key in failures:
            blockers.append({"kind": "failed_execution", "detail": str(failures[key])[:200]})
        kinds = {b["kind"] for b in blockers}
        catalog_state, _ = _state(
            authorized="unauthorized" not in kinds,
            pack_enabled="disabled_provider" not in kinds,
            import_error="blocked" if kinds & {"failed_execution", "inaccessible_data", "missing_credentials",
                                                "unverified_live_access", "aggregate_limit_exhausted"} else None,
            host_state=None,
            data_state="empty" if "empty_data" in kinds else data_state if data_state == "degraded" else "available",
            backend_ready=True,
        )
        omissions = [{k: v for k, v in o.items() if k != "pack"} for o in view.plan.get("omissions") or []
                     if o["pack"] in tool.consumers and o["reason"] != "not selected"]
        entry = {"capability": tool.capability, "operation": tool.operation, "provider": tool.provider,
                 "state": _CATALOG_TO_READINESS.get(catalog_state, "blocked"),
                 "prerequisites": sorted([f"probe:{p}" for p in tool.required_data]
                                         + [f"scope:{s}" for s in tool.required_scopes]
                                         + [f"context:{c}" for c in tool.required_context]),
                 "blockers": blockers}
        if omissions:
            entry["optional_omissions"] = omissions
        operations.append(entry)
    context = {"namespace": namespace}
    if principal_ref:
        context["principal_ref"] = principal_ref
    document = {"contract": READINESS_CONTRACT, "plan_digest": view.plan["digest"], "context": context,
                "observed_at_ms": int(now_ms() if now_ms else time.time() * 1000), "operations": operations}
    return redact(document, secrets=[v for v in credentials.values() if isinstance(v, str)])


# ------------------------------------------------------------ C04.5 redaction


def redact(payload: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Drop credential-bearing fields and scrub any known secret value from strings."""

    values = sorted({s for s in secrets if s}, key=len, reverse=True)

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: walk(v) for k, v in value.items() if str(k).casefold() not in SECRET_KEYS}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str):
            for secret in values:
                value = value.replace(secret, REDACTED)
            return value
        return value

    return walk(payload)
