"""Deterministic composition resolver (C03).

A pure function from an *explicit* candidate set — composition manifests and
provider descriptors already validated by :mod:`src.composition.contracts` —
plus root selections to an exact ``noesis-composition-plan-v1`` document.

Nothing here performs I/O: no database, network, filesystem or catalog reads.
Callers load candidates and hand them in; the resolver never discovers,
fetches or floats anything. Every iteration runs over sorted keys, so the
plan (and its digest) is byte-identical for any ordering of the inputs.

Model
-----
* A pack ``requires`` a capability under a contract name and an explicit
  range. Requirements bind to *provider descriptors* offering that
  capability; the pack that *contributes* the capability (if another
  candidate does) joins the closure so its declarations activate with it.
* Optional features join only when selected (root ``features``; for packs
  joined transitively, the manifest ``default``). A selected feature whose
  branch cannot resolve becomes a visible omission, never a silent gap.
* Versions: highest satisfying version, ties broken by ``content_hash``,
  using the schema registry range grammar (``contracts.satisfies``). A
  compatible pin from a retained plan is kept; an incompatible one fails
  rather than silently upgrading unless the caller names it in ``upgrade``.
* One version per provider identity per plan. Binding reason is
  ``explicit`` (a configured selection or the contributing pack's declared
  provider) or ``only-compatible``; more than one compatible provider is an
  ambiguity, never a pick.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.composition.contracts import (
    PLAN_CONTRACT,
    canonical_json,
    content_hash,
    plan_digest,
    satisfies,
    seal_plan,
    valid_range,
    validate_provider_set,
)

RESOLVER_VERSION = "1.0.0"
FAILURE_CODES = (
    "unknown_pack", "unknown_feature", "invalid_range", "cycle", "incompatible_range", "missing_contract",
    "missing_provider", "conflicting_major", "incompatible_retained_pin", "ambiguous_binding",
    "semantic_mismatch", "contract_mismatch", "conflicting_store_owner", "undeclared_binding", "alias_conflict",
)
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class ResolutionFailure:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


@dataclass(frozen=True)
class Resolution:
    plan: dict[str, Any] | None = None
    failure: ResolutionFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


class _Fail(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.failure = ResolutionFailure(code, message, details)


def _semver(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match(str(version))
    if not match:
        raise _Fail("invalid_range", f"version {version!r} is not an exact semantic version", version=version)
    return int(match[1]), int(match[2]), int(match[3])


def _hash(document: Mapping[str, Any]) -> str:
    return str(document.get("content_hash") or content_hash(document))


def _order(document: Mapping[str, Any]) -> tuple[tuple[int, int, int], str]:
    return _semver(document["version"]), _hash(document)


def _best(documents: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Highest version; equal versions break ties on the smaller content hash."""

    ordered = sorted(documents, key=lambda d: (tuple(-p for p in _semver(d["version"])), _hash(d)))
    return ordered[0]


def _ref(document: Mapping[str, Any]) -> str:
    return f"{document['id']}@{document['version']}"


def _capability(descriptor: Mapping[str, Any], capability: str) -> Mapping[str, Any] | None:
    for entry in descriptor.get("capabilities") or []:
        if entry.get("id") == capability:
            return entry
    return None


def _check_range(spec: str, *, pack: str, capability: str | None = None) -> None:
    if not valid_range(spec):
        raise _Fail("invalid_range", f"{pack}: range {spec!r} is not an explicit range", pack=pack,
                    capability=capability, range=spec)


@dataclass
class _Requirement:
    consumer: str
    feature: str | None
    capability: str
    contract: str
    range: str
    semantic_constraints: Mapping[str, Any]
    input_contract: str | None
    output_contract: str | None
    provider_ids: tuple[str, ...] = ()
    explicit: str | None = None

    @classmethod
    def of(cls, consumer: str, feature: str | None, data: Mapping[str, Any]) -> _Requirement:
        return cls(consumer, feature, str(data["capability"]), str(data["contract"]), str(data["range"]),
                   dict(data.get("semantic_constraints") or {}), data.get("input_contract"),
                   data.get("output_contract"))

    def label(self) -> str:
        where = f"{self.consumer}[{self.feature}]" if self.feature else self.consumer
        return f"{where} requires {self.capability} ({self.contract} {self.range})"

    def accepts(self, descriptor: Mapping[str, Any]) -> bool:
        entry = _capability(descriptor, self.capability)
        return bool(entry and entry["contract"]["name"] == self.contract
                    and satisfies(entry["contract"]["version"], self.range)
                    and not self._semantic_mismatches(entry) and self._io_matches(descriptor, entry))

    def _semantic_mismatches(self, entry: Mapping[str, Any]) -> list[str]:
        offered = entry.get("semantic_constraints") or {}
        return sorted(k for k, v in self.semantic_constraints.items() if offered.get(k) != v)

    def _io_matches(self, descriptor: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
        if not self.input_contract and not self.output_contract:
            return True
        operations = {op["id"]: op for op in descriptor.get("operations") or []}
        for op_id in entry.get("operations") or []:
            op = operations.get(op_id) or {}
            if ((not self.input_contract or op.get("input_contract") == self.input_contract)
                    and (not self.output_contract or op.get("output_contract") == self.output_contract)):
                return True
        return False


class _Resolver:
    def __init__(self, candidates: Sequence[Mapping[str, Any]], providers: Sequence[Mapping[str, Any]],
                 selections: Mapping[str, str], retained: Mapping[str, Any] | None,
                 upgrade: Iterable[str]) -> None:
        self.packs: dict[str, list[Mapping[str, Any]]] = {}
        for manifest in sorted(candidates, key=canonical_json):
            self.packs.setdefault(str(manifest["id"]), []).append(manifest)
        self.providers: dict[str, list[Mapping[str, Any]]] = {}
        for descriptor in sorted(providers, key=canonical_json):
            self.providers.setdefault(str(descriptor["id"]), []).append(descriptor)
        self.aliases: dict[str, set[str]] = {}
        for pack_id, versions in self.packs.items():
            for manifest in versions:
                for alias in manifest.get("compatibility_aliases") or {}:
                    self.aliases.setdefault(alias, set()).add(pack_id)
        self.selections = dict(selections)
        self.pack_pins: dict[str, tuple[str, str]] = {}
        self.provider_pins: dict[str, tuple[str, str]] = {}
        if retained:
            self.pack_pins = {p["id"]: (p["version"], p["content_hash"]) for p in retained.get("packs") or []}
            self.provider_pins = {p["id"]: (p["version"], p["content_hash"])
                                  for p in retained.get("providers") or []}
        self.upgrade = set(upgrade)
        self.selected: dict[str, Mapping[str, Any]] = {}
        self.features: dict[str, list[str]] = {}
        self.requirements: list[_Requirement] = []
        self.omissions: list[dict[str, Any]] = []
        self.pack_edges: dict[str, set[str]] = {}
        self.graph: list[dict[str, Any]] = []

    # ------------------------------------------------------------ packs

    def canonical_pack(self, name: str) -> str:
        if name in self.packs:
            return name
        targets = self.aliases.get(name) or set()
        if len(targets) > 1:
            raise _Fail("alias_conflict", f"alias {name!r} names more than one pack: {sorted(targets)}",
                        alias=name, packs=sorted(targets))
        if not targets:
            raise _Fail("unknown_pack", f"pack {name!r} is not in the candidate set", pack=name)
        return next(iter(targets))

    def choose_pack(self, pack_id: str, specs: Sequence[tuple[str, str]]) -> Mapping[str, Any]:
        versions = self.packs[pack_id]
        fits = [m for m in versions if all(satisfies(m["version"], spec) for spec, _ in specs)]
        pin = self.pack_pins.get(pack_id)
        if pin and pack_id not in self.upgrade:
            pinned = [m for m in versions if (m["version"], _hash(m)) == pin]
            if not pinned:
                raise _Fail("incompatible_retained_pin",
                            f"retained pin {pack_id}@{pin[0]} ({pin[1]}) is no longer a candidate",
                            pack=pack_id, version=pin[0], content_hash=pin[1])
            if pinned[0] not in fits:
                spec, who = next((s, w) for s, w in specs if not satisfies(pin[0], s))
                raise _Fail("incompatible_retained_pin",
                            f"{who} needs {pack_id} {spec} but the retained plan pins {pin[0]}; "
                            "name it in upgrade to move the pin", pack=pack_id, version=pin[0], range=spec)
            return pinned[0]
        if not fits:
            spec, who = specs[-1]
            offered = sorted({m["version"] for m in versions}, key=_semver)
            raise _Fail("incompatible_range", f"{who} needs {pack_id} {spec}; candidates offer {offered}",
                        pack=pack_id, range=spec, offered=offered)
        return _best(fits)

    def select_features(self, manifest: Mapping[str, Any], requested: Iterable[str] | None) -> list[str]:
        declared = {f["id"]: f for f in manifest.get("optional_features") or []}
        if requested is None:
            return sorted(f for f, spec in declared.items() if spec.get("default"))
        unknown = sorted(set(requested) - set(declared))
        if unknown:
            raise _Fail("unknown_feature", f"{_ref(manifest)} declares no optional feature {unknown[0]!r}",
                        pack=manifest["id"], feature=unknown[0])
        return sorted(set(requested))

    # ------------------------------------------------------------ requirements

    def compatible(self, req: _Requirement) -> tuple[str, ...]:
        """Provider identities with at least one version satisfying ``req``.

        Failure classes, most specific first: no descriptor offers the
        capability or its contract name (missing_contract), none in range
        (incompatible_range), none matching the semantics
        (semantic_mismatch) or the I/O contracts (contract_mismatch).
        """

        offers = [d for ds in self.providers.values() for d in ds if _capability(d, req.capability)]
        if not offers:
            raise _Fail("missing_contract", f"{req.label()}: no candidate provider declares {req.capability}",
                        pack=req.consumer, capability=req.capability, contract=req.contract)
        named = [d for d in offers if _capability(d, req.capability)["contract"]["name"] == req.contract]
        if not named:
            names = sorted({_capability(d, req.capability)["contract"]["name"] for d in offers})
            raise _Fail("missing_contract", f"{req.label()}: providers offer contracts {names}",
                        pack=req.consumer, capability=req.capability, contract=req.contract, offered=names)
        explicit = req.explicit
        if explicit:
            if not any(d["id"] == explicit for d in offers):
                raise _Fail("undeclared_binding",
                            f"{req.label()}: selected provider {explicit!r} does not declare {req.capability}",
                            pack=req.consumer, capability=req.capability, provider=explicit)
            named = [d for d in named if d["id"] == explicit]
        in_range = [d for d in named if satisfies(_capability(d, req.capability)["contract"]["version"], req.range)]
        if not in_range:
            offered = sorted(f"{_ref(d)} offers {_capability(d, req.capability)['contract']['version']}"
                             for d in named)
            raise _Fail("incompatible_range", f"{req.label()}: {'; '.join(offered) or 'nothing in range'}",
                        pack=req.consumer, capability=req.capability, range=req.range, offered=offered)
        semantic = [d for d in in_range if not req._semantic_mismatches(_capability(d, req.capability))]
        if not semantic:
            first = in_range[0]
            constraint = req._semantic_mismatches(_capability(first, req.capability))[0]
            offered = (_capability(first, req.capability).get("semantic_constraints") or {}).get(constraint)
            raise _Fail("semantic_mismatch",
                        f"{req.label()}: {_ref(first)} has {constraint}={offered!r}, "
                        f"needs {req.semantic_constraints[constraint]!r}",
                        pack=req.consumer, capability=req.capability, constraint=constraint,
                        provider=first["id"], version=first["version"])
        matching = [d for d in semantic if req._io_matches(d, _capability(d, req.capability))]
        if not matching:
            raise _Fail("contract_mismatch",
                        f"{req.label()}: no operation of {sorted({_ref(d) for d in semantic})} has input "
                        f"{req.input_contract!r} / output {req.output_contract!r}",
                        pack=req.consumer, capability=req.capability, input_contract=req.input_contract,
                        output_contract=req.output_contract)
        return tuple(sorted({d["id"] for d in matching}))

    def contributor(self, req: _Requirement) -> Mapping[str, Any] | None:
        """The pack contributing ``req.capability``: already selected, or the one candidate that does."""

        def contribution(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
            for entry in (manifest.get("contributes") or {}).get("capabilities") or []:
                if entry.get("id") == req.capability:
                    return entry
            return None

        def fits(manifest: Mapping[str, Any]) -> bool:
            entry = contribution(manifest) or {}
            contract = entry.get("contract")
            return not contract or (contract["name"] == req.contract and satisfies(contract["version"], req.range))

        for pack_id, manifest in sorted(self.selected.items()):
            if contribution(manifest):
                if not fits(manifest):
                    raise _Fail("incompatible_range",
                                f"{req.label()}: selected {_ref(manifest)} contributes "
                                f"{contribution(manifest).get('contract')}",
                                pack=req.consumer, capability=req.capability, version=manifest["version"])
                return manifest
        contributors = sorted({pack_id for pack_id, versions in self.packs.items()
                               if any(contribution(m) for m in versions)})
        if not contributors:
            return None
        if len(contributors) > 1:
            raise _Fail("ambiguous_binding",
                        f"{req.label()}: packs {contributors} all contribute it; select one as a root",
                        pack=req.consumer, capability=req.capability, candidates=contributors)
        pack_id = contributors[0]
        versions = [m for m in self.packs[pack_id] if contribution(m)]
        if not any(fits(m) for m in versions):
            offered = sorted(f"{_ref(m)} contributes {contribution(m).get('contract')}" for m in versions)
            raise _Fail("incompatible_range", f"{req.label()}: {'; '.join(offered)}", pack=req.consumer,
                        capability=req.capability, range=req.range, offered=offered)
        pin = self.pack_pins.get(pack_id)
        if pin and pack_id not in self.upgrade:
            held = self.choose_pack(pack_id, [(pin[0], req.label())])
            if not fits(held):
                raise _Fail("incompatible_retained_pin",
                            f"{req.label()} but the retained plan pins {_ref(held)}; name it in upgrade to move the pin",
                            pack=pack_id, capability=req.capability, version=held["version"])
            return held
        return _best([m for m in versions if fits(m)])

    def trial(self, consumer: str, feature: str | None, raw: Sequence[Mapping[str, Any]]):
        """Check a group of requirements without committing; raises on the first failure."""

        checked = []
        for data in sorted(raw, key=canonical_json):
            req = _Requirement.of(consumer, feature, data)
            _check_range(req.range, pack=consumer, capability=req.capability)
            joined = self.contributor(req)
            declared = None
            if joined is not None:
                for entry in (joined.get("contributes") or {}).get("capabilities") or []:
                    if entry.get("id") == req.capability:
                        declared = entry.get("provider")
            req.explicit = self.selections.get(req.capability) or declared
            req.provider_ids = self.compatible(req)
            checked.append((req, joined))
        return checked

    def commit(self, checked, queue: list[str]) -> None:
        for req, joined in checked:
            self.requirements.append(req)
            kind = "feature-requires" if req.feature else "requires"
            self.graph.append({"from": req.consumer, "to": req.capability, "kind": kind,
                               "capability": req.capability, "optional": bool(req.feature)})
            if joined is None or joined["id"] == req.consumer:
                continue
            self.pack_edges.setdefault(req.consumer, set()).add(joined["id"])
            if joined["id"] not in self.selected:
                self.selected[joined["id"]] = joined
                self.features[joined["id"]] = self.select_features(joined, None)
                queue.append(joined["id"])

    # ------------------------------------------------------------ closure

    def close(self, roots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for root in sorted(roots, key=canonical_json):
            pack_id = self.canonical_pack(str(root["pack"]))
            spec = str(root.get("version") or root.get("range") or "")
            _check_range(spec, pack=pack_id)
            entry = grouped.setdefault(pack_id, {"specs": [], "features": None})
            entry["specs"].append((spec, f"root {root['pack']}"))
            if root.get("features") is not None:
                entry["features"] = sorted(set(entry["features"] or []) | set(root["features"]))
        root_records = []
        for pack_id, entry in sorted(grouped.items()):
            manifest = self.choose_pack(pack_id, entry["specs"])
            self.selected[pack_id] = manifest
            self.features[pack_id] = self.select_features(manifest, entry["features"])
            root_records.append(pack_id)
        queue = sorted(root_records)
        while queue:
            queue.sort()
            pack_id = queue.pop(0)
            manifest = self.selected[pack_id]
            self.commit(self.trial(pack_id, None, manifest.get("requires") or []), queue)
            chosen = set(self.features[pack_id])
            for feature in sorted(manifest.get("optional_features") or [], key=lambda f: f["id"]):
                if feature["id"] not in chosen:
                    self.omissions.append({"pack": pack_id, "feature": feature["id"], "reason": "not selected"})
                    continue
                try:
                    checked = self.trial(pack_id, feature["id"], feature.get("requires") or [])
                except _Fail as failed:
                    if failed.failure.code in {"invalid_range", "alias_conflict"}:
                        raise
                    detail = failed.failure.details
                    self.omissions.append({"pack": pack_id, "feature": feature["id"],
                                           "capability": str(detail.get("capability") or ""),
                                           "reason": f"unavailable ({failed.failure.code}): "
                                                     f"{failed.failure.message}"})
                    self.features[pack_id] = sorted(chosen - {feature["id"]})
                    continue
                self.commit(checked, queue)
        return [{"pack": p, "version": self.selected[p]["version"], "content_hash": _hash(self.selected[p]),
                 "features": list(self.features[p])} for p in sorted(root_records)]

    def check_cycles(self) -> None:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(pack_id: str) -> None:
            state[pack_id] = 1
            stack.append(pack_id)
            for nxt in sorted(self.pack_edges.get(pack_id, ())):
                if state.get(nxt) == 1:
                    cycle = stack[stack.index(nxt):] + [nxt]
                    path = [_ref(self.selected[p]) for p in cycle]
                    raise _Fail("cycle", "dependency cycle: " + " -> ".join(path), path=path)
                if nxt not in state:
                    visit(nxt)
            stack.pop()
            state[pack_id] = 2

        for pack_id in sorted(self.selected):
            if pack_id not in state:
                visit(pack_id)

    # ------------------------------------------------------------ bindings

    def bind(self) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]]]:
        by_capability: dict[str, list[_Requirement]] = {}
        for req in self.requirements:
            by_capability.setdefault(req.capability, []).append(req)
        for capability, provider in sorted(self.selections.items()):
            if capability not in by_capability:
                raise _Fail("undeclared_binding",
                            f"provider selection {capability} -> {provider} binds a capability no selected "
                            "pack requires", capability=capability, provider=provider)
        chosen: dict[str, str] = {}
        reasons: dict[str, str] = {}
        for capability, reqs in sorted(by_capability.items()):
            common = set.intersection(*(set(r.provider_ids) for r in reqs))
            if not common:
                labels = sorted(f"{r.label()} -> {list(r.provider_ids)}" for r in reqs)
                raise _Fail("incompatible_range", f"no single provider satisfies every consumer of {capability}: "
                            + "; ".join(labels), capability=capability, consumers=labels)
            explicit = sorted({r.explicit for r in reqs if r.explicit})
            if len(explicit) > 1:
                raise _Fail("ambiguous_binding", f"{capability} has conflicting explicit providers {explicit}",
                            capability=capability, candidates=explicit)
            if len(common) > 1 and not explicit:
                raise _Fail("ambiguous_binding",
                            f"{capability} is offered by {sorted(common)}; configure one explicitly",
                            capability=capability, candidates=sorted(common),
                            consumers=sorted({r.consumer for r in reqs}))
            chosen[capability] = explicit[0] if explicit else next(iter(common))
            reasons[capability] = "explicit" if explicit else "only-compatible"
        pinned = self.pin_providers({c: [r for r in by_capability[c]] for c in chosen}, chosen)
        issues = [i for i in validate_provider_set(list(pinned.values())) if i.code == "conflicting_store_owner"]
        if issues:
            raise _Fail("conflicting_store_owner", issues[0].message, path=issues[0].path)
        bindings = []
        for capability, provider_id in sorted(chosen.items()):
            descriptor = pinned[provider_id]
            entry = _capability(descriptor, capability)
            bindings.append({"capability": capability,
                             "contract": {"name": entry["contract"]["name"], "version": entry["contract"]["version"]},
                             "provider": provider_id, "provider_version": descriptor["version"],
                             "operations": sorted(entry["operations"]), "reason": reasons[capability],
                             "consumers": sorted({r.consumer for r in by_capability[capability]})})
        return bindings, pinned

    def pin_providers(self, reqs_by_capability: Mapping[str, list[_Requirement]],
                      chosen: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
        """One version per provider identity satisfying every requirement bound to it."""

        needs: dict[str, list[_Requirement]] = {}
        for capability, provider_id in chosen.items():
            needs.setdefault(provider_id, []).extend(reqs_by_capability[capability])
        pinned: dict[str, Mapping[str, Any]] = {}
        for provider_id, reqs in sorted(needs.items()):
            versions = self.providers[provider_id]
            fits = [d for d in versions if all(r.accepts(d) for r in reqs)]
            pin = self.provider_pins.get(provider_id)
            if pin and provider_id not in self.upgrade:
                held = [d for d in versions if (d["version"], _hash(d)) == pin]
                if not held:
                    raise _Fail("incompatible_retained_pin",
                                f"retained pin {provider_id}@{pin[0]} ({pin[1]}) is no longer a candidate",
                                provider=provider_id, version=pin[0], content_hash=pin[1])
                if held[0] not in fits:
                    bad = next(r for r in sorted(reqs, key=lambda r: r.label()) if not r.accepts(held[0]))
                    raise _Fail("incompatible_retained_pin",
                                f"{bad.label()} but the retained plan pins {provider_id}@{pin[0]}; "
                                "name it in upgrade to move the pin",
                                pack=bad.consumer, capability=bad.capability, provider=provider_id, version=pin[0])
                pinned[provider_id] = held[0]
                continue
            if fits:
                pinned[provider_id] = _best(fits)
                continue
            per_req = {r.label(): _best([d for d in versions if r.accepts(d)]) for r in reqs
                       if any(r.accepts(d) for d in versions)}
            majors = sorted({_semver(d["version"])[0] for d in per_req.values()})
            detail = sorted(f"{label} -> {provider_id}@{d['version']}" for label, d in per_req.items())
            if len(majors) > 1:
                raise _Fail("conflicting_major",
                            f"{provider_id} would be needed at majors {majors}: " + "; ".join(detail),
                            provider=provider_id, majors=majors, requirements=detail)
            raise _Fail("incompatible_range", f"no single {provider_id} version satisfies: " + "; ".join(detail),
                        provider=provider_id, requirements=detail)
        return pinned

    # ------------------------------------------------------------ plan

    def plan(self, roots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        root_records = self.close(roots)
        self.check_cycles()
        for pack_id, manifest in sorted(self.selected.items()):
            for ref in (manifest.get("contributes") or {}).get("providers") or []:
                if not any(d["version"] == ref["version"] for d in self.providers.get(ref["id"], [])):
                    raise _Fail("missing_provider",
                                f"{_ref(manifest)} ships provider {ref['id']}@{ref['version']}, not a candidate",
                                pack=pack_id, provider=ref["id"], version=ref["version"])
        bindings, pinned = self.bind()
        aliases: dict[str, str] = {}
        for pack_id, manifest in sorted(self.selected.items()):
            for alias, target in sorted((manifest.get("compatibility_aliases") or {}).items()):
                if aliases.setdefault(alias, target) != target:
                    raise _Fail("alias_conflict", f"alias {alias!r} names both {aliases[alias]!r} and {target!r}",
                                alias=alias, packs=sorted({aliases[alias], target}))
        graph = list(self.graph)
        required = {r.capability for r in self.requirements}
        for pack_id, manifest in sorted(self.selected.items()):
            for entry in (manifest.get("contributes") or {}).get("capabilities") or []:
                if entry["id"] in required:
                    graph.append({"from": pack_id, "to": entry["id"], "kind": "contributes",
                                  "capability": entry["id"]})
        graph += [{"from": alias, "to": target, "kind": "alias"} for alias, target in sorted(aliases.items())]
        source_packs = {canonical_json(ref): dict(ref) for m in self.selected.values()
                        for ref in (m.get("contributes") or {}).get("source_packs") or []}
        body = {
            "contract": PLAN_CONTRACT,
            "resolver_version": RESOLVER_VERSION,
            "roots": root_records,
            "packs": [{"id": p, "version": m["version"], "content_hash": _hash(m)}
                      for p, m in sorted(self.selected.items())],
            "providers": [{"id": p, "version": d["version"], "content_hash": _hash(d)}
                          for p, d in sorted(pinned.items())],
            "source_packs": [source_packs[k] for k in sorted(source_packs)],
            "graph": _dedupe(graph),
            "features": {p: list(f) for p, f in sorted(self.features.items())},
            "bindings": bindings,
            "omissions": _dedupe(self.omissions),
            "output_contracts": [{"capability": b["capability"], **b["contract"]} for b in bindings],
            "compatibility_aliases": dict(sorted(aliases.items())),
        }
        return seal_plan(body)


def _dedupe(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {canonical_json(item): dict(item) for item in items}
    return [unique[key] for key in sorted(unique)]


def resolve(roots: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]],
            providers: Sequence[Mapping[str, Any]], *, selections: Mapping[str, str] | None = None,
            retained: Mapping[str, Any] | None = None, upgrade: Iterable[str] = ()) -> Resolution:
    """Resolve root selections over an explicit candidate set into a sealed plan.

    ``roots``: ``[{"pack": id-or-alias, "range"|"version": spec, "features": [...]}]``;
    ``selections``: explicit ``{capability: provider_id}``; ``retained``: a
    prior plan whose compatible pins are preserved; ``upgrade``: pack or
    provider IDs whose retained pins may move.
    """

    try:
        return Resolution(plan=_Resolver(candidates, providers, selections or {}, retained, upgrade).plan(roots))
    except _Fail as failed:
        return Resolution(failure=failed.failure)


# ------------------------------------------------------------ C03.4 resume


@dataclass(frozen=True)
class ResumeResult:
    status: str  # current | new_plan_required | unavailable_for_replay | invalid_plan
    diff: list[dict[str, Any]] = field(default_factory=list)
    unavailable: list[dict[str, Any]] = field(default_factory=list)
    replan: Resolution | None = None


def resume(plan: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]],
           providers: Sequence[Mapping[str, Any]]) -> ResumeResult:
    """Check that every hash a plan pins still exists in the candidate set.

    Unchanged pins → ``current``. A pinned pack or provider whose identity is
    still present but at a different version or content hash (a changed
    descriptor, contract or manifest) → ``new_plan_required`` with the diff
    and a re-resolution that moves only the changed pins. An identity that is
    gone entirely → ``unavailable_for_replay``: the historical plan can be
    inspected but not replayed, and nothing is substituted.
    """

    if plan.get("digest") != plan_digest(plan):
        return ResumeResult("invalid_plan", diff=[{"kind": "digest", "pinned": plan.get("digest"),
                                                   "available": plan_digest(plan)}])
    diff: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for kind, pins, pool in (("pack", plan.get("packs") or [], candidates),
                             ("provider", plan.get("providers") or [], providers)):
        index: dict[str, list[dict[str, str]]] = {}
        for document in pool:
            index.setdefault(str(document["id"]), []).append({"version": document["version"],
                                                              "content_hash": _hash(document)})
        for pin in sorted(pins, key=lambda p: p["id"]):
            available = sorted(index.get(pin["id"], []), key=canonical_json)
            pinned = {"version": pin["version"], "content_hash": pin["content_hash"]}
            if not available:
                unavailable.append({"kind": kind, "id": pin["id"], "pinned": pinned})
            elif pinned not in available:
                diff.append({"kind": kind, "id": pin["id"], "pinned": pinned, "available": available})
    if unavailable:
        return ResumeResult("unavailable_for_replay", diff=diff, unavailable=unavailable)
    if not diff:
        return ResumeResult("current")
    roots = [{"pack": r["pack"], "range": f"^{r['version']}" if _semver(r["version"])[0] else r["version"],
              "features": r["features"]} for r in plan["roots"]]
    selections = {b["capability"]: b["provider"] for b in plan.get("bindings") or [] if b["reason"] == "explicit"}
    replan = resolve(roots, candidates, providers, selections=selections, retained=plan,
                     upgrade={d["id"] for d in diff})
    if replan.ok:
        before = {b["capability"]: b for b in plan.get("bindings") or []}
        after = {b["capability"]: b for b in replan.plan["bindings"]}
        for capability in sorted(set(before) | set(after)):
            if before.get(capability) != after.get(capability):
                diff.append({"kind": "binding", "id": capability, "pinned": before.get(capability),
                             "available": after.get(capability)})
    return ResumeResult("new_plan_required", diff=diff, replan=replan)
