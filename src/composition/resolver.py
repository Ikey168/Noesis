"""Pure, deterministic composition resolution (slice C03, #1810-#1813).

:func:`resolve` turns root selections into a ``noesis-composition-plan-v1``
document over an explicit candidate set, or fails with a specific error.
:func:`check_resume` decides whether a stored plan can still be executed.

Purity is a contract, not a convention. This module performs no I/O: it reads
no registry, file, network or database, and imports only the pure helpers of
:mod:`src.composition.contracts`. Inputs are plain validated documents
(C02); the same inputs always yield a byte-identical digest regardless of the
order in which candidates or roots are given.

Rules (``docs/architecture/pack-workflow-composition.md``):

* Required capabilities resolve transitively: a capability bound to a provider
  contributed by another pack pulls that pack (and its requirements) in.
* Optional features join only when selected. A selected but unavailable
  optional branch is a visible omission, never an error.
* Versions use the schema registry's range semantics. Compatible retained pins
  survive re-resolution; an incompatible retained pin fails instead of
  silently upgrading. Floating ranges never appear in a plan.
* A capability binds to the explicitly configured provider, else the only
  compatible one. Several compatible providers without a choice yield an
  ambiguity result naming them; order is never a tie-breaker.
* A contract range is not proof of compatibility: input/output contracts,
  declared semantics and effects must also match.
* One active version per provider identity: candidates with two major versions
  of one provider block resolution.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.composition.contracts import (
    PLAN_CONTRACT,
    CompositionError,
    check_range,
    manifest_id,
    plan_digest,
    satisfies,
    version_key,
)

RESOLVER_VERSION = "1"


def _provider_key(descriptor: Mapping[str, Any]) -> str:
    return f"{descriptor['provider_id']}@{descriptor['version']}"


def _sorted_unique(items: Iterable[str]) -> list[str]:
    return sorted(set(items))


class _Candidates:
    """Indexed view over the explicit candidate set."""

    def __init__(
        self,
        manifests: Sequence[Mapping[str, Any]],
        providers: Sequence[Mapping[str, Any]],
        contracts: Mapping[str, Sequence[str]],
        source_packs: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    ) -> None:
        self.manifests: dict[str, list[Mapping[str, Any]]] = {}
        for manifest in manifests:
            self.manifests.setdefault(manifest["name"], []).append(manifest)
        for versions in self.manifests.values():
            versions.sort(key=lambda m: version_key(m["version"]))
        self.providers: dict[str, list[Mapping[str, Any]]] = {}
        for descriptor in providers:
            self.providers.setdefault(descriptor["provider_id"], []).append(descriptor)
        for versions in self.providers.values():
            versions.sort(key=lambda d: version_key(d["version"]))
        self.contracts = {name: sorted(set(v), key=version_key) for name, v in contracts.items()}
        self.source_packs = (
            None
            if source_packs is None
            else {
                pack_id: sorted(versions, key=lambda v: version_key(v["version"]))
                for pack_id, versions in source_packs.items()
            }
        )
        # Which manifest version contributes which provider version.
        self.contributors: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for manifest in manifests:
            for ref in manifest["contributes"].get("providers", []):
                self.contributors.setdefault(
                    (ref["provider_id"], ref["version"]), []
                ).append(manifest)
        self.declared_capabilities = {
            cap["capability"] for d in providers for cap in d["capabilities"]
        }


def _major_conflict(candidates: _Candidates, provider_id: str) -> None:
    majors = {version_key(d["version"])[0] for d in candidates.providers.get(provider_id, [])}
    if len(majors) > 1:
        raise CompositionError(
            "conflicting_major_versions",
            f"provider {provider_id} is offered in major versions {sorted(majors)}; "
            "one active version per provider identity is supported",
            provider_id=provider_id,
            majors=sorted(majors),
        )


def _capability(descriptor: Mapping[str, Any], capability: str) -> Mapping[str, Any] | None:
    for item in descriptor["capabilities"]:
        if item["capability"] == capability:
            return item
    return None


def _incompatibility(
    requirement: Mapping[str, Any], offered: Mapping[str, Any], contracts: _Candidates
) -> str | None:
    """Why ``offered`` cannot satisfy ``requirement``, or None when it can."""

    if not satisfies(offered["version"], requirement["range"]):
        return f"capability version {offered['version']} is outside {requirement['range']}"
    for key in ("input_contract", "output_contract"):
        wanted = requirement.get(key)
        actual = offered[key]
        if wanted is None:
            continue
        if wanted["name"] != actual["name"]:
            return f"{key} is {actual['name']}, not {wanted['name']}"
        if not satisfies(actual["version"], wanted["range"]):
            return f"{key} {actual['name']}@{actual['version']} is outside {wanted['range']}"
    semantics = offered.get("semantics", {})
    for name, value in sorted(requirement.get("semantics", {}).items()):
        if semantics.get(name) != value:
            return (
                f"semantic constraint {name}={value!r} differs from the provider's "
                f"{name}={semantics.get(name)!r}"
            )
    effects = requirement.get("effects")
    if effects and offered["effect"] not in effects:
        return f"effect {offered['effect']} is not among the declared effects {sorted(effects)}"
    for key in ("input_contract", "output_contract"):
        contract = offered[key]
        if contract["version"] not in contracts.contracts.get(contract["name"], []):
            return f"{key} {contract['name']}@{contract['version']} is not in the candidate contracts"
    return None


def _pick_manifest(
    candidates: _Candidates,
    name: str,
    spec: str,
    retained: Mapping[str, str],
    *,
    consumer: str,
) -> Mapping[str, Any]:
    versions = candidates.manifests.get(name, [])
    if not versions:
        raise CompositionError(
            "missing_pack", f"{consumer}: pack {name} is not in the candidate set", pack=name
        )
    compatible = [m for m in versions if satisfies(m["version"], spec)]
    pinned = retained.get(name)
    if pinned is not None:
        match = [m for m in versions if m["version"] == pinned]
        if not match:
            raise CompositionError(
                "retained_pin_unavailable",
                f"{consumer}: retained pin {name}@{pinned} is no longer in the candidate set",
                pack=name,
                version=pinned,
            )
        if not satisfies(pinned, spec):
            raise CompositionError(
                "incompatible_retained_pin",
                f"{consumer}: retained pin {name}@{pinned} is outside {spec}",
                pack=name,
                version=pinned,
                range=spec,
            )
        return match[0]
    if not compatible:
        raise CompositionError(
            "incompatible_range",
            f"{consumer}: no version of pack {name} satisfies {spec} "
            f"(candidates {[m['version'] for m in versions]})",
            pack=name,
            range=spec,
        )
    return compatible[-1]


def resolve(
    *,
    roots: Sequence[Mapping[str, str]],
    manifests: Sequence[Mapping[str, Any]],
    providers: Sequence[Mapping[str, Any]],
    contracts: Mapping[str, Sequence[str]],
    features: Mapping[str, Sequence[str]] | None = None,
    provider_choices: Mapping[str, str] | None = None,
    source_packs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    retained: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve root selections over an explicit candidate set.

    Returns ``{"status": "resolved", "plan": plan}`` or
    ``{"status": "ambiguous", "ambiguities": [...]}``; raises
    :class:`CompositionError` for cycles, incompatibilities, missing contracts,
    conflicting ownership, undeclared bindings and conflicting majors.

    ``roots`` are ``{"name", "range"}`` selections; ``features`` maps a pack
    name to its selected optional features; ``provider_choices`` maps a
    capability ID to the configured provider ID; ``source_packs`` maps a
    source ``pack_id`` to retained ``{"version", "manifest_hash"}`` entries;
    ``retained`` is a prior plan whose compatible pins are preserved.
    """

    selected_features = {name: set(values) for name, values in (features or {}).items()}
    choices = dict(provider_choices or {})
    candidates = _Candidates(manifests, providers, contracts, source_packs)
    retained_packs = {m["name"]: m["version"] for m in (retained or {}).get("manifests", [])}
    retained_bindings = {
        (b["consumer"].split("@", 1)[0], b["capability"]): (b["provider_id"], b["provider_version"])
        for b in (retained or {}).get("bindings", [])
    }
    for root in roots:
        check_range(root["range"])
    for capability, provider_id in choices.items():
        if provider_id not in candidates.providers:
            raise CompositionError(
                "missing_provider",
                f"configured provider {provider_id} for {capability} is not in the candidate set",
                capability=capability,
                provider_id=provider_id,
            )

    selected: dict[str, Mapping[str, Any]] = {}
    pack_edges: dict[str, set[str]] = {}
    bindings: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    chosen_providers: dict[str, Mapping[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()
    source_refs: dict[str, str] = {}
    source_ranges: dict[str, set[str]] = {}

    def choose_provider(
        consumer: Mapping[str, Any], requirement: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any] | None, str, str | None]:
        capability = requirement["capability"]
        if capability not in candidates.declared_capabilities:
            return None, "undeclared", None
        compatible: list[Mapping[str, Any]] = []
        reasons: dict[str, str] = {}
        for provider_id in sorted(candidates.providers):
            for descriptor in candidates.providers[provider_id]:
                offered = _capability(descriptor, capability)
                if offered is None:
                    continue
                why = _incompatibility(requirement, offered, candidates)
                if why is None:
                    compatible.append(descriptor)
                else:
                    reasons[_provider_key(descriptor)] = why
        if not compatible:
            detail = "; ".join(f"{key}: {why}" for key, why in sorted(reasons.items()))
            return None, "incompatible", detail
        for provider_id in {d["provider_id"] for d in compatible}:
            _major_conflict(candidates, provider_id)
        pinned = retained_bindings.get((consumer["name"], capability))
        configured = choices.get(capability)
        if pinned is not None:
            match = [
                d for d in compatible
                if d["provider_id"] == pinned[0] and d["version"] == pinned[1]
            ]
            if match and (configured is None or configured == pinned[0]):
                return match[0], "retained-pin", None
            if configured is None:
                raise CompositionError(
                    "incompatible_retained_pin",
                    f"{manifest_id(consumer)}: retained provider {pinned[0]}@{pinned[1]} "
                    f"for {capability} is no longer compatible or available",
                    capability=capability,
                    provider_id=pinned[0],
                    version=pinned[1],
                )
        ids = sorted({d["provider_id"] for d in compatible})
        if configured is not None:
            if configured not in ids:
                raise CompositionError(
                    "incompatible_provider",
                    f"{manifest_id(consumer)}: configured provider {configured} cannot "
                    f"satisfy {capability}: "
                    + "; ".join(
                        why for key, why in sorted(reasons.items()) if key.startswith(configured + "@")
                    ),
                    capability=capability,
                    provider_id=configured,
                )
            pool = [d for d in compatible if d["provider_id"] == configured]
            return pool[-1], "explicit", None
        if len(ids) > 1:
            ambiguities.append(
                {
                    "consumer": manifest_id(consumer),
                    "capability": capability,
                    "providers": [
                        _provider_key(max(
                            (d for d in compatible if d["provider_id"] == pid),
                            key=lambda d: version_key(d["version"]),
                        ))
                        for pid in ids
                    ],
                }
            )
            return None, "ambiguous", None
        return max(compatible, key=lambda d: version_key(d["version"])), "only-compatible", None

    def lock_source(consumer: str, ref: Mapping[str, Any], optional: bool) -> bool:
        if candidates.source_packs is None:
            return True
        versions = candidates.source_packs.get(ref["pack_id"], [])
        spec = ref.get("range")
        matching = [v for v in versions if spec is None or satisfies(v["version"], spec)]
        if not matching:
            if optional:
                return False
            raise CompositionError(
                "missing_source_pack",
                f"{consumer}: source pack {ref['pack_id']} {spec or ''} is not retained",
                pack_id=ref["pack_id"],
            )
        chosen = matching[-1]
        previous = source_refs.get(ref["pack_id"])
        key = f"{chosen['version']}|{chosen.get('manifest_hash', '')}"
        if previous is not None and previous != key:
            raise CompositionError(
                "incompatible_range",
                f"{consumer}: source pack {ref['pack_id']} is pinned to two versions",
                pack_id=ref["pack_id"],
            )
        source_refs[ref["pack_id"]] = key
        if spec:
            source_ranges.setdefault(ref["pack_id"], set()).add(spec)
        edges.add((consumer, f"source:{ref['pack_id']}@{chosen['version']}", "sources"))
        return True

    def visit(name: str, spec: str, *, via: str, stack: list[str]) -> str:
        manifest = _pick_manifest(candidates, name, spec, retained_packs, consumer=via)
        node = manifest_id(manifest)
        if node in stack:
            cycle = stack[stack.index(node):] + [node]
            raise CompositionError(
                "dependency_cycle",
                "dependency cycle: " + " -> ".join(cycle),
                cycle=cycle,
            )
        existing = selected.get(manifest["name"])
        if existing is not None:
            if existing["version"] != manifest["version"]:
                raise CompositionError(
                    "incompatible_range",
                    f"pack {name} is required at {existing['version']} and {manifest['version']}",
                    pack=name,
                )
            return node
        selected[manifest["name"]] = manifest
        pack_edges.setdefault(node, set())
        feature_set = selected_features.get(manifest["name"], set())
        unknown = feature_set - set(manifest.get("optional_features", {}))
        if unknown:
            raise CompositionError(
                "unknown_feature",
                f"{node}: selected features {sorted(unknown)} are not declared",
                pack=manifest["name"],
            )
        for ref in manifest.get("references", {}).get("source_packs", []):
            lock_source(node, ref, optional=False)
        for requirement in sorted(
            manifest["requires"], key=lambda r: (r["capability"], r.get("feature") or "")
        ):
            feature = requirement.get("feature")
            if feature is not None and feature not in feature_set:
                omissions.append(
                    {"consumer": node, "capability": requirement["capability"],
                     "feature": feature, "code": "feature-not-selected"}
                )
                continue
            descriptor, reason, detail = choose_provider(manifest, requirement)
            if descriptor is None:
                if reason == "ambiguous":
                    continue
                if feature is not None:
                    omissions.append(
                        {"consumer": node, "capability": requirement["capability"],
                         "feature": feature, "code": f"unavailable-{reason}"}
                    )
                    continue
                if reason == "undeclared":
                    raise CompositionError(
                        "undeclared_binding",
                        f"{node}: no provider in the candidate set declares "
                        f"{requirement['capability']}",
                        consumer=node,
                        capability=requirement["capability"],
                    )
                raise CompositionError(
                    "incompatible_provider",
                    f"{node}: no provider satisfies {requirement['capability']} "
                    f"{requirement['range']}: {detail}",
                    consumer=node,
                    capability=requirement["capability"],
                )
            offered = _capability(descriptor, requirement["capability"])
            provider_node = _provider_key(descriptor)
            prior = chosen_providers.get(descriptor["provider_id"])
            if prior is not None and prior["version"] != descriptor["version"]:
                raise CompositionError(
                    "conflicting_major_versions"
                    if version_key(prior["version"])[0] != version_key(descriptor["version"])[0]
                    else "incompatible_range",
                    f"provider {descriptor['provider_id']} would be active at "
                    f"{prior['version']} and {descriptor['version']}",
                    provider_id=descriptor["provider_id"],
                )
            chosen_providers[descriptor["provider_id"]] = descriptor
            bindings.append(
                {
                    "consumer": node,
                    "capability": requirement["capability"],
                    "range": requirement["range"],
                    "provider_id": descriptor["provider_id"],
                    "provider_version": descriptor["version"],
                    "capability_version": offered["version"],
                    "effect": offered["effect"],
                    "idempotent": bool(offered["idempotent"]),
                    "execution_receipt": bool(offered.get("execution_receipt", False)),
                    "reason": reason,
                    "input_contract": dict(offered["input_contract"]),
                    "output_contract": dict(offered["output_contract"]),
                    "bindings": sorted(
                        (dict(b) for b in offered["bindings"]),
                        key=lambda b: (b["kind"], b["id"]),
                    ),
                    **({"feature": feature} if feature else {}),
                }
            )
            edges.add((node, provider_node, "binds"))
            contributors = candidates.contributors.get(
                (descriptor["provider_id"], descriptor["version"]), []
            )
            if contributors:
                owner = max(contributors, key=lambda m: version_key(m["version"]))
                owner_node = manifest_id(owner)
                edges.add((owner_node, provider_node, "contributes"))
                if owner["name"] != manifest["name"]:
                    pack_edges[node].add(owner_node)
                    visit(owner["name"], owner["version"], via=node, stack=[*stack, node])
            for ref in descriptor.get("source_packs", []):
                lock_source(provider_node, ref, optional=feature is not None)
        return node

    for root in sorted(roots, key=lambda r: (r["name"], r["range"])):
        visit(root["name"], root["range"], via=f"root {root['name']}", stack=[])

    if ambiguities:
        return {
            "status": "ambiguous",
            "ambiguities": sorted(ambiguities, key=lambda a: (a["consumer"], a["capability"])),
        }

    owners: dict[str, str] = {}
    for descriptor in chosen_providers.values():
        for store in descriptor["stores"]:
            owner = owners.setdefault(store["record_kind"], descriptor["provider_id"])
            if owner != descriptor["provider_id"]:
                raise CompositionError(
                    "conflicting_store_owner",
                    f"record kind {store['record_kind']} is owned by both {owner} "
                    f"and {descriptor['provider_id']}",
                    record_kind=store["record_kind"],
                    providers=sorted({owner, descriptor["provider_id"]}),
                )

    contract_pins: set[tuple[str, str]] = set()
    for binding in bindings:
        for key in ("input_contract", "output_contract"):
            contract_pins.add((binding[key]["name"], binding[key]["version"]))
    for manifest in selected.values():
        for ref in manifest.get("references", {}).get("contracts", []):
            versions = candidates.contracts.get(ref["name"])
            if not versions:
                raise CompositionError(
                    "missing_contract",
                    f"{manifest_id(manifest)}: contract {ref['name']} is not in the candidate set",
                    consumer=manifest_id(manifest),
                    contract=ref["name"],
                )
            matching = [v for v in versions if satisfies(v, ref["range"])]
            if not matching:
                raise CompositionError(
                    "incompatible_range",
                    f"{manifest_id(manifest)}: contract {ref['name']} has no version in "
                    f"{ref['range']} (candidates {versions})",
                    consumer=manifest_id(manifest),
                    contract=ref["name"],
                )
            contract_pins.add((ref["name"], matching[-1]))

    aliases = {
        manifest_id(m): dict(m["aliases"]["capability_labels"])
        for m in selected.values()
        if m.get("aliases", {}).get("capability_labels")
    }
    plan: dict[str, Any] = {
        "contract": PLAN_CONTRACT,
        "resolver_version": RESOLVER_VERSION,
        "roots": [
            {"name": root["name"], "version": selected[root["name"]]["version"]}
            for root in roots
        ],
        "features": {
            name: sorted(selected_features.get(name, set())) for name in sorted(selected)
        },
        "manifests": [
            {"name": m["name"], "version": m["version"], "manifest_hash": m["manifest_hash"]}
            for m in selected.values()
        ],
        "providers": [
            {"provider_id": d["provider_id"], "version": d["version"],
             "descriptor_hash": d["descriptor_hash"]}
            for d in chosen_providers.values()
        ],
        "contracts": [{"name": name, "version": version} for name, version in contract_pins],
        "source_packs": [
            {"pack_id": pack_id, "version": pin.split("|", 1)[0],
             "ranges": sorted(source_ranges.get(pack_id, set())),
             **({"manifest_hash": pin.split("|", 1)[1]} if pin.split("|", 1)[1] else {})}
            for pack_id, pin in source_refs.items()
        ],
        "bindings": bindings,
        "edges": [{"from": a, "to": b, "kind": kind} for a, b, kind in edges],
        "omissions": omissions,
        "aliases": aliases,
        "output_contracts": [
            {"capability": b["capability"], **b["output_contract"]} for b in bindings
        ],
        "conservative": _sorted_unique(
            manifest_id(m) for m in selected.values()
            if {"requires", "store_ownership"} & set(m.get("adapter", {}).get("supplied_defaults", []))
        ),
    }
    plan = _canonical_plan(plan)
    plan["digest"] = plan_digest(plan)
    return {"status": "resolved", "plan": plan}


def _canonical_plan(plan: dict[str, Any]) -> dict[str, Any]:
    from src.composition.contracts import canonicalize_plan

    return canonicalize_plan(plan)


def check_resume(
    plan: Mapping[str, Any],
    *,
    manifests: Sequence[Mapping[str, Any]],
    providers: Sequence[Mapping[str, Any]],
    contracts: Mapping[str, Sequence[str]],
    source_packs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decide whether a stored plan can resume against the current candidates.

    ``source_packs`` maps a source ``pack_id`` to its currently installed
    ``{"version", "manifest_hash"}``; a pinned source that moved requires a new
    plan (historical runs keep their pins; new execution re-resolves).

    ``current`` when every pinned manifest, provider and contract is still
    present with the same hash. ``new-plan-required`` with the differences when
    something changed but is still offered. ``unavailable-for-replay`` when a
    pinned provider identity is gone. A resume never re-resolves ranges.
    """

    if plan.get("digest") != plan_digest(plan):
        raise CompositionError("plan_digest_mismatch", "stored plan does not match its digest")
    offered_manifests = {(m["name"], m["version"]): m["manifest_hash"] for m in manifests}
    offered_names = {m["name"] for m in manifests}
    offered_providers = {(d["provider_id"], d["version"]): d["descriptor_hash"] for d in providers}
    offered_ids = {d["provider_id"] for d in providers}
    changes: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for pin in plan["manifests"]:
        current = offered_manifests.get((pin["name"], pin["version"]))
        if current == pin["manifest_hash"]:
            continue
        entry = {"kind": "manifest", "id": f"{pin['name']}@{pin['version']}",
                 "pinned_hash": pin["manifest_hash"], "current_hash": current}
        (changes if pin["name"] in offered_names else unavailable).append(entry)
    for pin in plan["providers"]:
        current = offered_providers.get((pin["provider_id"], pin["version"]))
        if current == pin["descriptor_hash"]:
            continue
        entry = {"kind": "provider", "id": f"{pin['provider_id']}@{pin['version']}",
                 "pinned_hash": pin["descriptor_hash"], "current_hash": current,
                 "offered_versions": sorted(
                     (v for (pid, v) in offered_providers if pid == pin["provider_id"]),
                     key=version_key,
                 )}
        (changes if pin["provider_id"] in offered_ids else unavailable).append(entry)
    for pin in plan["contracts"]:
        if pin["version"] not in contracts.get(pin["name"], []):
            changes.append({"kind": "contract", "id": f"{pin['name']}@{pin['version']}",
                            "offered_versions": list(contracts.get(pin["name"], []))})
    for pin in plan.get("source_packs", []):
        if source_packs is None:
            break
        current = source_packs.get(pin["pack_id"])
        if current is None:
            unavailable.append({"kind": "source_pack", "id": f"{pin['pack_id']}@{pin['version']}"})
        elif current["version"] != pin["version"] or (
            pin.get("manifest_hash") and current.get("manifest_hash") != pin["manifest_hash"]
        ):
            changes.append({"kind": "source_pack", "id": f"{pin['pack_id']}@{pin['version']}",
                            "current_version": current["version"]})
    if unavailable:
        return {"status": "unavailable-for-replay", "plan_digest": plan["digest"],
                "unavailable": sorted(unavailable, key=lambda e: (e["kind"], e["id"])),
                "changes": sorted(changes, key=lambda e: (e["kind"], e["id"]))}
    if changes:
        return {"status": "new-plan-required", "plan_digest": plan["digest"],
                "changes": sorted(changes, key=lambda e: (e["kind"], e["id"]))}
    return {"status": "current", "plan_digest": plan["digest"]}


__all__ = ["RESOLVER_VERSION", "check_resume", "resolve"]
