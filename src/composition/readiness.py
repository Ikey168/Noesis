"""Operation-specific readiness and composition explanations (slice C04).

* :func:`assess` evaluates each bound operation of a plan under one caller's
  context and returns a ``noesis-composition-readiness-v1`` observation
  (C04.2). It is never stored as a plan fact.
* :func:`explain` answers, per capability and per workflow: which provider is
  bound, which visible packs consume it, why it was selected, what is pinned,
  what data it needs, and why it is blocked or degraded (C04.3).
* :class:`CompositionView` gives the catalog plan-derived tool bindings and
  pack attribution (C04.1) and maps readiness onto catalog states.

Redaction (C04.5) happens at emission: blockers carry a kind from a closed set
and a fixed reason, credential blockers name only the credential kind, and
consumers are limited to packs the caller may see. The readiness schema also
forbids the fields that would carry anything else.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.composition.contracts import READINESS_CONTRACT, CompositionError, validate_readiness

# Catalog state precedence mirrors src/mcp_host/catalog.py:_state.
_KIND_TO_STATE = {
    "unauthorized": "unauthorized",
    "provider-disabled": "disabled",
    "provider-unavailable": "unavailable",
    "binding-missing": "unavailable",
    "empty-data": "empty",
    "inaccessible-data": "degraded",
    "missing-credentials": "degraded",
    "unverified-live": "degraded",
    "failed-execution": "degraded",
    "aggregate-limit-exhausted": "degraded",
    "provider-shutdown": "unavailable",
}
_STATE_ORDER = ("unauthorized", "disabled", "unavailable", "empty", "degraded", "available")

# The probe that delegates data readiness to the legacy catalog evaluation.
# Tools behind it keep the legacy state; only coordinator-owned blockers
# (below) can override it, because the legacy tables cannot see them.
DELEGATING_PROBE = "catalog.required-data"
LIFECYCLE_BLOCKERS = frozenset(
    {"provider-disabled", "provider-shutdown", "failed-execution", "aggregate-limit-exhausted"}
)

# Fixed, content-free reasons per blocker kind. Emission uses these instead of
# any caller- or probe-supplied text, so nothing private can leak through them.
REASONS = {
    "empty-data": "required data is empty",
    "inaccessible-data": "required data is not accessible to this caller",
    "missing-credentials": "a required credential is not configured",
    "provider-disabled": "the provider is disabled",
    "unverified-live": "live access has not been verified",
    "failed-execution": "the last execution of this operation failed",
    "unauthorized": "required scope is not granted",
    "provider-unavailable": "the provider is not available",
    "binding-missing": "the operation has no registered binding",
    "aggregate-limit-exhausted": "the shared provider account limit is exhausted",
    "provider-shutdown": "the provider was shut down by an administrator",
}


def _scope_digest(scopes: Iterable[str]) -> str:
    return "sha256:" + hashlib.sha256("\n".join(sorted(set(scopes))).encode()).hexdigest()


def _operation_id(binding: Mapping[str, Any]) -> str:
    targets = binding.get("bindings") or []
    mcp = [b["id"] for b in targets if b["kind"] == "mcp-tool"]
    registered = [b["id"] for b in targets if b["kind"] == "registered"]
    return (mcp or registered or [binding["capability"]])[0]


def _blocker(kind: str, **extra: Any) -> dict[str, Any]:
    entry = {"kind": kind, "reason": REASONS[kind]}
    for key in ("credential_kind", "record_kind"):
        if extra.get(key):
            entry[key] = str(extra[key])[:64]
    return entry


def assess(
    plan: Mapping[str, Any],
    providers: Iterable[Mapping[str, Any]],
    *,
    conn: Any = None,
    namespace: str = "global",
    scopes: Iterable[str] = (),
    network: str = "disabled",
    credential_available: Callable[[str], bool] | None = None,
    disabled_providers: Iterable[str] = (),
    unavailable_providers: Iterable[str] = (),
    shutdown_providers: Iterable[str] = (),
    failed_operations: Iterable[str] = (),
    exhausted_accounts: Iterable[str] = (),
    probe_results: Mapping[str, Mapping[str, Any]] | None = None,
    visible_consumers: Iterable[str] | None = None,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    """Readiness of every bound operation in ``plan`` for one caller.

    ``credential_available`` answers only whether a credential kind is
    configured; values never reach this function. ``probe_results`` overrides
    registered probes (used in tests and by callers that already observed).
    ``visible_consumers`` limits which consumers appear; ``None`` means every
    root of the plan is visible.
    """

    from src.composition import bindings as registry

    scope_set = set(scopes)
    descriptors = {(d["provider_id"], d["version"]): d for d in providers}
    disabled = set(disabled_providers)
    unavailable = set(unavailable_providers)
    shutdown = set(shutdown_providers)
    failed = set(failed_operations)
    exhausted = set(exhausted_accounts)
    visible = None if visible_consumers is None else set(visible_consumers)
    probe_cache: dict[str, Mapping[str, Any]] = {}
    operations = []
    for binding in plan["bindings"]:
        if visible is not None and binding["consumer"] not in visible:
            continue
        descriptor = descriptors.get((binding["provider_id"], binding["provider_version"]))
        operation = _operation_id(binding)
        prerequisites: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        entry = {
            "consumer": binding["consumer"],
            "capability": binding["capability"],
            "operation": operation,
            "provider_id": binding["provider_id"],
            "effect": binding["effect"],
        }
        if descriptor is None:
            blockers.append(_blocker("provider-unavailable"))
            operations.append({**entry, "state": "blocked", "prerequisites": [], "blockers": blockers})
            continue
        offered = next(
            c for c in descriptor["capabilities"] if c["capability"] == binding["capability"]
        )
        required = list(offered.get("required_scopes", []))
        granted = "operator" in scope_set or set(required) <= scope_set
        for scope in required:
            prerequisites.append(
                {"kind": "scope", "name": scope,
                 "satisfied": "operator" in scope_set or scope in scope_set}
            )
        if not granted:
            blockers.append(_blocker("unauthorized"))
        context = set(offered.get("required_context", []))
        if "namespace" in context:
            prerequisites.append({"kind": "context", "name": "namespace", "satisfied": bool(namespace)})
        enabled = binding["provider_id"] not in disabled
        prerequisites.append({"kind": "provider-enabled", "name": binding["provider_id"], "satisfied": enabled})
        if not enabled:
            blockers.append(_blocker("provider-disabled"))
        if binding["provider_id"] in unavailable:
            blockers.append(_blocker("provider-unavailable"))
        if binding["provider_id"] in shutdown:
            blockers.append(_blocker("provider-shutdown"))
        if "credential" in context:
            ok = bool(credential_available and credential_available(binding["provider_id"]))
            prerequisites.append({"kind": "credential", "name": binding["provider_id"], "satisfied": ok})
            if not ok:
                blockers.append(_blocker("missing-credentials", credential_kind="provider-account"))
        if binding["effect"] == "acquisition" or "network" in context:
            live = network == "live"
            prerequisites.append({"kind": "live-access", "name": "network", "satisfied": live})
            if not live:
                blockers.append(_blocker("unverified-live"))
        if binding["provider_id"] in exhausted:
            blockers.append(_blocker("aggregate-limit-exhausted"))
        known = registry.registered_binding_ids()
        for target in binding.get("bindings", []):
            if target["kind"] == "registered" and target["id"] not in known:
                blockers.append(_blocker("binding-missing"))
        probe_id = offered["readiness"]["probe"]
        cache_key = f"{probe_id}|{','.join(offered['readiness'].get('required_data', []))}"
        if probe_results is not None and probe_id in probe_results:
            observed = probe_results[probe_id]
        elif cache_key in probe_cache:
            observed = probe_cache[cache_key]
        else:
            probe = registry.probe(probe_id)
            observed = (
                {"state": "blocked", "blockers": [{"kind": "binding-missing"}]}
                if probe is None
                else probe.fn(conn, {"namespace": namespace, "network": network,
                                     "required_data": offered["readiness"].get("required_data", [])})
            )
            probe_cache[cache_key] = observed
        prerequisites.append(
            {"kind": "data", "name": probe_id, "satisfied": observed.get("state") == "ready"}
        )
        for item in observed.get("blockers", []):
            kind = item.get("kind")
            if kind in REASONS:
                blockers.append(_blocker(kind, credential_kind=item.get("credential_kind"),
                                         record_kind=item.get("record_kind")))
        if operation in failed:
            blockers.append(_blocker("failed-execution"))
        unique = []
        for blocker in blockers:
            if blocker not in unique:
                unique.append(blocker)
        state = "ready" if not unique else (
            "blocked" if any(_KIND_TO_STATE[b["kind"]] != "degraded" for b in unique) else "degraded"
        )
        operations.append({**entry, "state": state, "prerequisites": prerequisites, "blockers": unique})
    omissions = [
        {"consumer": o["consumer"], "capability": o["capability"], "feature": o["feature"],
         "code": o["code"]}
        for o in plan.get("omissions", [])
        if visible is None or o["consumer"] in visible
    ]
    for omission in omissions:
        operations.append({
            "consumer": omission["consumer"], "capability": omission["capability"],
            "operation": omission["capability"], "provider_id": None, "state": "omitted",
            "prerequisites": [], "blockers": [],
        })
    summary: dict[str, int] = {}
    for item in operations:
        summary[item["state"]] = summary.get(item["state"], 0) + 1
    assessment = {
        "contract": READINESS_CONTRACT,
        "plan_digest": plan["digest"],
        "context": {"namespace": namespace, "scope_digest": _scope_digest(scope_set),
                    "network": network if network in {"disabled", "live"} else "disabled"},
        "observed_at_ms": int(observed_at_ms if observed_at_ms is not None else time.time() * 1000),
        "operations": sorted(operations, key=lambda o: (o["consumer"], o["capability"], o["operation"])),
        "optional_omissions": omissions,
        "summary": dict(sorted(summary.items())),
    }
    return validate_readiness(assessment)


def catalog_state(operation: Mapping[str, Any]) -> tuple[str, str | None]:
    """Map one readiness operation onto a catalog state and reason."""

    if operation["state"] == "ready":
        return "available", None
    kinds = [b["kind"] for b in operation["blockers"]]
    states = [_KIND_TO_STATE[k] for k in kinds] or ["degraded"]
    state = min(states, key=_STATE_ORDER.index)
    reason = next(REASONS[k] for k in kinds if _KIND_TO_STATE[k] == state) if kinds else None
    return state, reason


class CompositionView:
    """Plan-derived tool bindings and pack attribution for the catalog (C04.1).

    Tools bound in the plan take their pack attribution and data prerequisites
    from the plan and descriptors; every other tool keeps the legacy tables.
    """

    def __init__(
        self,
        plan: Mapping[str, Any],
        providers: Iterable[Mapping[str, Any]],
        *,
        readiness: Mapping[str, Any] | None = None,
        visible_consumers: Iterable[str] | None = None,
    ) -> None:
        self.plan = plan
        self.providers = {(d["provider_id"], d["version"]): d for d in providers}
        self.readiness = readiness
        self.visible = None if visible_consumers is None else set(visible_consumers)
        contributors: dict[str, set[str]] = {}
        for edge in plan["edges"]:
            if edge["kind"] == "contributes":
                contributors.setdefault(edge["to"], set()).add(edge["from"])
        self.tools: dict[str, dict[str, Any]] = {}
        for binding in plan["bindings"]:
            provider_node = f"{binding['provider_id']}@{binding['provider_version']}"
            descriptor = self.providers.get((binding["provider_id"], binding["provider_version"]))
            offered = None if descriptor is None else next(
                c for c in descriptor["capabilities"] if c["capability"] == binding["capability"]
            )
            for target in binding.get("bindings", []):
                if target["kind"] != "mcp-tool":
                    continue
                entry = self.tools.setdefault(
                    target["id"],
                    {"capability": binding["capability"], "provider_id": binding["provider_id"],
                     "provider_version": binding["provider_version"], "reason": binding["reason"],
                     "consumers": set(), "contributors": set(contributors.get(provider_node, set())),
                     "required_data": list((offered or {}).get("readiness", {}).get("required_data", [])),
                     "probe": (offered or {}).get("readiness", {}).get("probe")},
                )
                entry["consumers"].add(binding["consumer"])

    def bound(self, tool_id: str) -> bool:
        return tool_id in self.tools

    def _visible(self, consumers: Iterable[str]) -> list[str]:
        return sorted(c for c in consumers if self.visible is None or c in self.visible)

    def packs(self, tool_id: str) -> list[str]:
        """Pack attribution for a bound tool: visible consumers and contributors."""

        entry = self.tools[tool_id]
        names = {c.split("@", 1)[0] for c in self._visible(entry["consumers"])}
        names |= {c.split("@", 1)[0] for c in entry["contributors"]}
        return sorted(names)

    def required_data(self, tool_id: str, legacy: list[str]) -> list[str]:
        declared = self.tools[tool_id]["required_data"]
        return list(declared) if declared else list(legacy)

    def delegated(self, tool_id: str) -> bool:
        """Whether the tool's data readiness is the legacy catalog evaluation."""

        return self.tools[tool_id]["probe"] == DELEGATING_PROBE

    def lifecycle_state(self, tool_id: str) -> tuple[str, str | None] | None:
        """A coordinator-owned blocker on the tool's provider, if any."""

        if self.readiness is None:
            return None
        provider = self.tools[tool_id]["provider_id"]
        kinds = sorted({
            b["kind"] for o in self.readiness["operations"] if o.get("provider_id") == provider
            for b in o["blockers"] if b["kind"] in LIFECYCLE_BLOCKERS
        })
        if not kinds:
            return None
        state = min((_KIND_TO_STATE[k] for k in kinds), key=_STATE_ORDER.index)
        return state, next(REASONS[k] for k in kinds if _KIND_TO_STATE[k] == state)

    def operation(self, tool_id: str) -> Mapping[str, Any] | None:
        if self.readiness is None:
            return None
        matches = [o for o in self.readiness["operations"] if o["operation"] == tool_id]
        if not matches:
            return None
        return min(matches, key=lambda o: _STATE_ORDER.index(catalog_state(o)[0]))

    def explanation(self, tool_id: str) -> dict[str, Any]:
        entry = self.tools[tool_id]
        result = {
            "capability": entry["capability"],
            "provider": {"provider_id": entry["provider_id"], "version": entry["provider_version"]},
            "selection_reason": entry["reason"],
            "consumers": self._visible(entry["consumers"]),
            "plan_digest": self.plan["digest"],
        }
        operation = self.operation(tool_id)
        if operation is not None:
            result["readiness"] = {"state": operation["state"],
                                   "blockers": [dict(b) for b in operation["blockers"]]}
        return result


def explain(
    plan: Mapping[str, Any],
    providers: Iterable[Mapping[str, Any]],
    readiness: Mapping[str, Any],
    *,
    templates: Iterable[Mapping[str, Any]] = (),
    visible_consumers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Composition explanations per capability and per workflow (C04.3)."""

    if readiness["plan_digest"] != plan["digest"]:
        raise CompositionError("stale_readiness", "readiness was observed for a different plan")
    visible = None if visible_consumers is None else set(visible_consumers)
    descriptors = {(d["provider_id"], d["version"]): d for d in providers}
    pins = {p["provider_id"]: p for p in plan["providers"]}
    capabilities: dict[str, dict[str, Any]] = {}
    for binding in plan["bindings"]:
        if visible is not None and binding["consumer"] not in visible:
            continue
        descriptor = descriptors.get((binding["provider_id"], binding["provider_version"]))
        offered = {} if descriptor is None else next(
            c for c in descriptor["capabilities"] if c["capability"] == binding["capability"]
        )
        item = capabilities.setdefault(binding["capability"], {
            "capability": binding["capability"],
            "provider_id": binding["provider_id"],
            "pinned": {"version": binding["provider_version"],
                       "descriptor_hash": pins[binding["provider_id"]]["descriptor_hash"],
                       "capability_version": binding["capability_version"]},
            "selection_reasons": set(),
            "consumers": set(),
            "required_data": list(offered.get("readiness", {}).get("required_data", [])),
            "effect": binding["effect"],
            "operations": [],
        })
        item["selection_reasons"].add(binding["reason"])
        item["consumers"].add(binding["consumer"])
    for operation in readiness["operations"]:
        if operation["capability"] in capabilities and operation["state"] != "omitted":
            item = capabilities[operation["capability"]]
            summary = {"operation": operation["operation"], "consumer": operation["consumer"],
                       "state": operation["state"], "blockers": [dict(b) for b in operation["blockers"]]}
            if summary not in item["operations"]:
                item["operations"].append(summary)
    rendered = []
    for name in sorted(capabilities):
        item = capabilities[name]
        states = {op["state"] for op in item["operations"]}
        rendered.append({
            **item,
            "selection_reasons": sorted(item["selection_reasons"]),
            "consumers": sorted(item["consumers"]),
            "consuming_packs": sorted({c.split("@", 1)[0] for c in item["consumers"]}),
            "state": "blocked" if "blocked" in states else "degraded" if "degraded" in states else "ready",
            "operations": sorted(item["operations"], key=lambda o: (o["consumer"], o["operation"])),
        })
    workflows = []
    by_capability = {item["capability"]: item for item in rendered}
    omitted = {(o["consumer"], o["capability"]) for o in readiness.get("optional_omissions", [])}
    for template in templates:
        consumer = template.get("consumer")
        if visible is not None and consumer is not None and consumer not in visible:
            continue
        required = [s["capability"] for s in template.get("steps", []) if not s.get("optional")]
        optional = [s["capability"] for s in template.get("steps", []) if s.get("optional")]
        blocking = []
        degraded = []
        for capability in required:
            item = by_capability.get(capability)
            if item is None:
                blocking.append({"capability": capability, "operation": None,
                                 "blocker": {"kind": "binding-missing",
                                             "reason": REASONS["binding-missing"]}})
                continue
            for op in item["operations"]:
                if consumer is not None and op["consumer"] != consumer:
                    continue
                target = blocking if op["state"] == "blocked" else degraded if op["state"] == "degraded" else None
                if target is not None:
                    target.extend({"capability": capability, "operation": op["operation"], "blocker": b}
                                  for b in op["blockers"])
        workflows.append({
            "template_id": template["template_id"],
            "version": template["version"],
            "state": "blocked" if blocking else "degraded" if degraded else "ready",
            "blocking": blocking,
            "degraded": degraded,
            "optional_omissions": sorted(
                c for c in optional if consumer is None or (consumer, c) in omitted
            ),
        })
    return {
        "plan_digest": plan["digest"],
        "observed_at_ms": readiness["observed_at_ms"],
        "capabilities": rendered,
        "workflows": sorted(workflows, key=lambda w: (w["template_id"], w["version"])),
    }


__all__ = ["REASONS", "CompositionView", "assess", "catalog_state", "explain"]
