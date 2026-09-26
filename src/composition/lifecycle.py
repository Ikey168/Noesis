"""Lifecycle coordinator: journal, generation switch, retention and authority (C05).

Activation runs four journaled stages (C05.2, C05.3):

1. **previewed** - resolve the current selection (retaining compatible pins of
   the active plan) and list the affected consumers;
2. **staged** - prepare registrations and provider bindings for the new
   generation without disturbing the active one;
3. **verified** - check staged registrations against the plan and observe
   readiness (an observation only);
4. **published** - in one transaction, write the generation, switch the
   active pointer and journal the stage; then apply in-process registrations.

A database transaction keeps the pointer and journal consistent; it does not
make Python registrations or external actions atomic. In-process state is a
pure function of the active generation and is rebuilt by :meth:`reconcile`
after a crash. No stage issues provider requests, acquires data or changes
schedules.

Retention (C05.4): providers stay bound while any remaining root or pinned
active run needs them; administrative shutdown is a separate, reported action;
uninstall refuses anything in use and never deletes evidence. Switching the
generation pointer back does not reverse a destructive provider data
migration; such migrations need their own compatibility and rollback plan.

Authority (C05.5): until a bundle is cut over, the legacy registry is its only
authority and the coordinator leaves its registrations alone. After cutover,
legacy calls delegate here or raise a compatibility error, and
:meth:`rollback` restores legacy registrations without touching source
versions, cursors or records. ``NOESIS_COMPOSITION_LIFECYCLE=legacy`` keeps
the authority hook uninstalled for the whole process.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.composition.contracts import (
    RECEIPT_CONTRACT,
    CompositionError,
    canonical,
    contract_candidates,
    digest,
    receipt_hash,
    v1_view,
    validate_receipt,
)
from src.composition.resolver import resolve
from src.composition.store import CompositionStore

STAGES = ("previewed", "staged", "verified", "published")
LEGACY_FLAG = "NOESIS_COMPOSITION_LIFECYCLE"


@dataclass
class Runtime:
    """In-process state derived from the active generation."""

    generation: int | None = None
    plan: dict[str, Any] | None = None
    providers: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    bundles: dict[str, str] = field(default_factory=dict)  # composition-applied name -> version
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)

    def bindings(self) -> list[dict[str, Any]]:
        return list((self.plan or {}).get("bindings", []))


_RUNTIME = Runtime()


def runtime() -> Runtime:
    return _RUNTIME


def reset_runtime() -> None:
    """Forget in-process state (a process restart in tests)."""

    global _RUNTIME
    _RUNTIME = Runtime()


def legacy_mode() -> bool:
    return os.getenv(LEGACY_FLAG, "").strip().lower() == "legacy"


def _registration(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The in-process registration a manifest implies (not yet applied)."""

    from src.domains.pack_format import PackManifest

    from src.composition.identifiers import code_registered_packs

    adapter = manifest.get("adapter", {})
    if adapter.get("source") == "domain-pack":
        return {"kind": "code-registered", "name": manifest["name"], "version": manifest["version"],
                "templates": []}
    view = PackManifest.from_dict(v1_view(manifest))
    if manifest["name"] in code_registered_packs():
        # A distributable manifest whose name a built-in domain module also
        # registers (legal, economics, political, technology): the module's
        # DomainPack keeps its routes and code enrichers; the manifest adds
        # only its provisioning templates.
        return {"kind": "code-registered", "name": manifest["name"], "version": manifest["version"],
                "templates": list(view.provisioning_templates)}
    return {"kind": "manifest", "name": manifest["name"], "version": manifest["version"], "v1": view}


def _apply_registration(registration: Mapping[str, Any]) -> None:
    from src.domains import pack_install
    from src.domains import registry as domain_registry
    from src.domains.base import DomainPack

    name = registration["name"]
    if registration["kind"] == "manifest":
        view = registration["v1"]
        pack = DomainPack(
            name=view.name,
            description=view.description,
            source_types=list(view.source_types),
            enrichers=[pack_install._compile_enricher(e) for e in view.enrichers],
            ui_flags=dict(view.ui_flags),
            capabilities=list(view.capabilities),
            schema_versions=dict(view.schema_versions),
            ontology_extensions=dict(view.ontology_extensions),
        )
        # Replacing by name is a single dict assignment: the old registration
        # serves until this line, the new one from this line on.
        domain_registry.register_pack(pack)
        for template in view.provisioning_templates:
            pack_install._TEMPLATES[template["name"]] = {**template, "pack": name}
    else:
        from src.composition.identifiers import code_registered_packs

        if domain_registry.get_pack(name) is None and name in code_registered_packs():
            domain_registry.register_pack(code_registered_packs()[name])
        for template in registration.get("templates", []):
            pack_install._TEMPLATES[template["name"]] = {**template, "pack": name}
    domain_registry._set_enabled(name, True)


def _remove_registration(name: str, version: str | None = None) -> None:
    from src.domains import pack_install
    from src.domains import registry as domain_registry

    for template_name, template in list(pack_install._TEMPLATES.items()):
        if template.get("pack") == name:
            pack_install._TEMPLATES.pop(template_name, None)
    domain_registry._set_enabled(name, False)


class Crash(BaseException):
    """Raised by test fault hooks to simulate process death at a stage."""


class Coordinator:
    """Drives activation for composition-managed packs over one warehouse connection."""

    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        contracts: Mapping[str, Iterable[str]] | None = None,
        fault: Callable[[str], None] | None = None,
        readiness_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = CompositionStore(conn, now=self.now)
        self._contracts = None if contracts is None else {k: list(v) for k, v in contracts.items()}
        self.fault = fault or (lambda _stage: None)
        self.readiness_context = dict(readiness_context or {})
        self.hooks: dict[str, list[Callable[..., None]]] = {"disable": [], "published": []}
        from src.composition.sources import install_hooks

        install_hooks(self)

    # -- candidates ---------------------------------------------------------
    def contracts(self) -> dict[str, list[str]]:
        if self._contracts is None:
            self._contracts = contract_candidates()
        return self._contracts

    def source_packs(self) -> dict[str, list[dict[str, Any]]] | None:
        """Currently installed source-pack versions (the source store stays authoritative)."""

        from src.composition.sources import source_pins

        try:
            self.conn.execute("SELECT 1 FROM source_pack_current LIMIT 1")
        except Exception:  # noqa: BLE001 - no source-pack store means no source pins
            return None
        return {pack_id: [pin] for pack_id, pin in source_pins(self.conn).items()}

    def _resolve(self, *, retain: bool) -> dict[str, Any]:
        selection = self.store.selection()
        choices: dict[str, str] = {}
        for item in selection.values():
            choices.update(item["provider_choices"])
        result = resolve(
            roots=[{"name": name, "range": item["range"]} for name, item in selection.items()],
            manifests=self.store.manifests(),
            providers=self.store.providers(),
            contracts=self.contracts(),
            features={name: item["features"] for name, item in selection.items()},
            provider_choices=choices,
            source_packs=self.source_packs(),
            retained=self.store.active_plan() if retain else None,
        )
        return result

    # -- preview ------------------------------------------------------------
    def preview(self, *, retain: bool = True) -> dict[str, Any]:
        """Dependency closure and affected consumers for the current selection."""

        result = self._resolve(retain=retain)
        if result["status"] != "resolved":
            return {"status": result["status"], "ambiguities": result.get("ambiguities", [])}
        plan = result["plan"]
        active = self.store.active_plan()
        return {"status": "resolved", "plan": plan, **_changes(active, plan)}

    # -- activation ---------------------------------------------------------
    def _journal(self, activation_id: str, stage: str, status: str, detail: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO composition_journal VALUES (?,?,?,?,?) "
            "ON CONFLICT (activation_id, stage) DO UPDATE SET status=excluded.status, "
            "detail_json=excluded.detail_json, at_ms=excluded.at_ms",
            [activation_id, stage, status, canonical(dict(detail)), self.now()],
        )

    def _receipt(self, activation_id: str, key: str, operation: str, *, previous: int | None,
                 new: int | None, plan_digest: str, registrations: Mapping[str, list[str]],
                 stages: list[dict[str, Any]], status: str, recovery: str,
                 principal_id: str, error: Mapping[str, Any] | None = None,
                 source_upgrade_refs: Iterable[str] = ()) -> dict[str, Any]:
        receipt = {
            "contract": RECEIPT_CONTRACT,
            "activation_id": activation_id,
            "idempotency_key": key,
            "operation": operation,
            "previous_generation": previous,
            "new_generation": new,
            "plan_digest": plan_digest,
            "registrations": {k: sorted(v) for k, v in registrations.items()},
            "source_upgrade_refs": sorted(source_upgrade_refs),
            "stages": stages,
            "status": status,
            "recovery_status": recovery,
            "error": None if error is None else {"code": str(error["code"]), "message": str(error["message"])[:500]},
            "principal_id": principal_id,
            "created_at_ms": self.now(),
        }
        receipt["receipt_hash"] = receipt_hash(receipt)
        return validate_receipt(receipt, generation_plans=self.store.generation_plans())

    def _begin(self, key: str, operation: str, request: Any) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise CompositionError("invalid_request", "a bounded idempotency key is required")
        activation_id = "composition-activation:" + digest([operation, key])[7:31]
        request_hash = digest(request)
        row = self.conn.execute(
            "SELECT request_hash, receipt_json FROM composition_activations WHERE idempotency_key=?",
            [key]).fetchone()
        if row:
            if row[0] != request_hash:
                raise CompositionError("idempotency_conflict", "idempotency key names another request")
            if row[1]:
                return activation_id, {**json.loads(row[1]), "idempotent": True}
            return activation_id, None
        self.conn.execute(
            "INSERT INTO composition_activations VALUES (?,?,?,?,NULL,?)",
            [activation_id, key, request_hash, operation, self.now()])
        return activation_id, None

    def _finish(self, activation_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        self.conn.execute("UPDATE composition_activations SET receipt_json=? WHERE activation_id=?",
                          [canonical(dict(receipt)), activation_id])
        return {**receipt, "idempotent": False}

    def activate(self, idempotency_key: str, *, principal_id: str, retain: bool = True,
                 operation: str = "activate", source_upgrade_refs: Iterable[str] = ()) -> dict[str, Any]:
        """Preview, stage, verify and publish a new generation for the current selection."""

        selection = self.store.selection()
        activation_id, prior = self._begin(
            idempotency_key, operation, {"selection": selection, "retain": retain,
                                         "refs": sorted(source_upgrade_refs)})
        if prior is not None:
            return prior
        active = self.store.active_generation()
        previous = None if active is None else active["generation"]
        previous_plan = self.store.active_plan()
        stages: list[dict[str, Any]] = []
        plan_digest = (previous_plan or {}).get("digest") or digest({})

        def failed(error: CompositionError) -> dict[str, Any]:
            self._journal(activation_id, "failed", "failed", error.as_dict())
            stages.append({"stage": "failed", "status": "failed", "detail": error.code})
            receipt = self._receipt(
                activation_id, idempotency_key, operation, previous=previous, new=None,
                plan_digest=plan_digest, registrations={"added": [], "removed": [], "retained": []},
                stages=stages, status="failed", recovery="previous-generation-retained",
                principal_id=principal_id, error={"code": error.code, "message": error.message},
                source_upgrade_refs=source_upgrade_refs)
            return self._finish(activation_id, receipt)

        try:
            preview = self.preview(retain=retain)
            if preview["status"] != "resolved":
                raise CompositionError("ambiguous_selection", "the selection does not resolve to one binding",
                                       ambiguities=preview.get("ambiguities", []))
        except CompositionError as error:
            return failed(error)
        plan = preview["plan"]
        plan_digest = plan["digest"]
        self.store.store_plan(plan, selection)
        self._journal(activation_id, "previewed", "applied",
                      {"plan_digest": plan_digest, "affected": preview["affected_consumers"],
                       "source_upgrade_refs": sorted(source_upgrade_refs)})
        stages.append({"stage": "previewed", "status": "applied"})
        self.fault("previewed")

        staged = self._stage(plan)
        self._journal(activation_id, "staged", "applied",
                      {"bundles": sorted(staged["bundles"]), "providers": sorted(staged["providers"])})
        stages.append({"stage": "staged", "status": "applied"})
        self.fault("staged")

        try:
            observation = self._verify(plan, staged)
        except CompositionError as error:
            return failed(error)
        self._journal(activation_id, "verified", "applied", {"readiness": observation})
        stages.append({"stage": "verified", "status": "applied"})
        self.fault("verified")

        new = (previous or 0) + 1
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("INSERT INTO composition_generations VALUES (?,?,?,?)",
                              [new, plan_digest, activation_id, self.now()])
            self.conn.execute(
                "INSERT INTO composition_active VALUES (1, ?, ?) ON CONFLICT (singleton) "
                "DO UPDATE SET generation=excluded.generation, updated_at_ms=excluded.updated_at_ms",
                [new, self.now()])
            self._journal(activation_id, "published", "applied", {"generation": new})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        stages.append({"stage": "published", "status": "applied"})
        self.fault("published")
        changes = self.apply_active()
        for hook in self.hooks["published"]:
            hook(previous_plan, plan)
        receipt = self._receipt(
            activation_id, idempotency_key, operation, previous=previous, new=new,
            plan_digest=plan_digest, registrations=changes, stages=stages, status="applied",
            recovery="not-needed", principal_id=principal_id, source_upgrade_refs=source_upgrade_refs)
        return self._finish(activation_id, receipt)

    def _stage(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        manifests = {(m["name"], m["version"]): m for m in self.store.manifests()}
        providers = {(d["provider_id"], d["version"]): d for d in self.store.providers()}
        bundles = {}
        for pin in plan["manifests"]:
            manifest = manifests.get((pin["name"], pin["version"]))
            if manifest is None or manifest["manifest_hash"] != pin["manifest_hash"]:
                raise CompositionError("stage_failed", f"manifest {pin['name']}@{pin['version']} changed")
            bundles[pin["name"]] = _registration(manifest)
        staged_providers = {}
        for pin in plan["providers"]:
            descriptor = providers.get((pin["provider_id"], pin["version"]))
            if descriptor is None or descriptor["descriptor_hash"] != pin["descriptor_hash"]:
                raise CompositionError("stage_failed", f"provider {pin['provider_id']} changed")
            staged_providers[pin["provider_id"]] = descriptor
        return {"bundles": bundles, "providers": staged_providers}

    def _verify(self, plan: Mapping[str, Any], staged: Mapping[str, Any]) -> dict[str, int]:
        pinned = {pin["name"] for pin in plan["manifests"]}
        if set(staged["bundles"]) != pinned:
            raise CompositionError("verify_failed", "staged registrations do not match the plan")
        for binding in plan["bindings"]:
            if binding["provider_id"] not in staged["providers"]:
                raise CompositionError("verify_failed", f"binding to unstaged provider {binding['provider_id']}")
        from src.composition.readiness import assess

        observation = assess(plan, staged["providers"].values(), conn=self.conn,
                             scopes={"operator"}, observed_at_ms=self.now(),
                             shutdown_providers=self.shutdown_providers(),
                             **self.readiness_context)
        return observation["summary"]

    # -- in-process state ---------------------------------------------------
    def managed(self) -> set[str]:
        if legacy_mode():
            return set()
        return {name for name, item in self.store.authority().items() if item["authority"] == "composition"}

    def apply_active(self) -> dict[str, list[str]]:
        """Rebuild in-process state from the active generation; idempotent."""

        global _RUNTIME
        active = self.store.active_generation()
        plan = None if active is None else self.store.plan(active["plan_digest"])
        previous_bundles = dict(_RUNTIME.bundles)
        providers: dict[tuple[str, str], dict[str, Any]] = {}
        needed = [plan] if plan else []
        needed += self.store.pinned_plans()
        stored = {(d["provider_id"], d["version"]): d for d in self.store.providers()}
        for item in needed:
            for pin in item["providers"]:
                key = (pin["provider_id"], pin["version"])
                if key in stored:
                    providers[key] = stored[key]
        managed = self.managed()
        manifests = {(m["name"], m["version"]): m for m in self.store.manifests()}
        target: dict[str, str] = {}
        templates: dict[str, dict[str, Any]] = {}
        for pin in (plan or {}).get("manifests", []):
            manifest = manifests.get((pin["name"], pin["version"]))
            if manifest is None:
                continue
            for template in manifest["contributes"].get("workflow_templates", []):
                templates[f"{template['template_id']}@{template['version']}"] = {
                    **template, "pack": pin["name"]}
            if pin["name"] in managed:
                target[pin["name"]] = pin["version"]
                _apply_registration(_registration(manifest))
        for name, version in previous_bundles.items():
            if name not in target:
                _remove_registration(name, version)
        added = sorted(f"bundle:{n}@{v}" for n, v in target.items() if previous_bundles.get(n) != v)
        removed = sorted(f"bundle:{n}@{v}" for n, v in previous_bundles.items() if target.get(n) != v)
        retained = sorted(f"bundle:{n}@{v}" for n, v in target.items() if previous_bundles.get(n) == v)
        old_providers = set(_RUNTIME.providers)
        added += sorted(f"provider:{p}@{v}" for p, v in providers if (p, v) not in old_providers)
        removed += sorted(f"provider:{p}@{v}" for p, v in old_providers if (p, v) not in providers)
        retained += sorted(f"provider:{p}@{v}" for p, v in providers if (p, v) in old_providers)
        _RUNTIME = Runtime(
            generation=None if active is None else active["generation"],
            plan=plan, providers=providers, bundles=target, templates=templates,
        )
        return {"added": added, "removed": removed, "retained": retained}

    def reconcile(self, *, principal_id: str = "system") -> dict[str, Any]:
        """Startup reconciliation within the ADR-003 boundary."""

        abandoned = []
        rows = self.conn.execute(
            "SELECT activation_id, list(stage) FROM composition_journal GROUP BY activation_id").fetchall()
        for activation_id, stages in rows:
            stages = set(stages)
            if stages & {"published", "failed", "abandoned", "rolled-back"}:
                continue
            refs = self._staged_source_refs(activation_id)
            self._journal(activation_id, "abandoned", "applied",
                          {"source_upgrades": refs, "previous_generation_retained": True})
            abandoned.append(activation_id)
        changes = self.apply_active()
        if not legacy_mode():
            install_authority(self)
        self.store.audit("reconcile", "composition", principal_id,
                         {"abandoned": abandoned, "generation": runtime().generation})
        return {"generation": runtime().generation, "abandoned": abandoned, "registrations": changes}

    def _staged_source_refs(self, activation_id: str) -> dict[str, str]:
        detail = self.conn.execute(
            "SELECT detail_json FROM composition_journal WHERE activation_id=? AND stage='previewed'",
            [activation_id]).fetchone()
        refs = json.loads(detail[0]).get("source_upgrade_refs", []) if detail else []
        result = {}
        for ref in refs:
            try:
                found = self.conn.execute(
                    "SELECT 1 FROM source_pack_upgrade_receipts WHERE apply_id=?", [ref]).fetchone()
            except Exception:  # noqa: BLE001 - no upgrade store means nothing was applied
                found = None
            result[ref] = "applied" if found else "not-applied"
        return result

    # -- C05.4 disable, shutdown, uninstall ----------------------------------
    def disable(self, root: str, idempotency_key: str, *, principal_id: str) -> dict[str, Any]:
        """Remove a root's selection and publish the reduced closure."""

        if root not in self.store.selection():
            raise CompositionError("not_selected", f"{root} is not selected")
        active = self.store.active_plan()
        self.store.deselect(root, principal_id=principal_id)
        receipt = self.activate(idempotency_key, principal_id=principal_id, operation="disable")
        if receipt["status"] == "applied":
            for hook in self.hooks["disable"]:
                hook(root, active, self.store.active_plan())
        return receipt

    def shutdown_providers(self) -> set[str]:
        return {pid for pid, state in self.store.provider_states().items() if state == "shutdown"}

    def shutdown_provider(self, provider_id: str, idempotency_key: str, *, reason: str,
                          principal_id: str) -> dict[str, Any]:
        """Administratively shut a provider down and report affected consumers."""

        activation_id, prior = self._begin(idempotency_key, "shutdown", {"provider": provider_id})
        if prior is not None:
            return prior
        plan = self.store.active_plan()
        affected = sorted({b["consumer"] for b in (plan or {}).get("bindings", [])
                           if b["provider_id"] == provider_id})
        self.store.set_provider_state(provider_id, "shutdown", reason=reason, principal_id=principal_id)
        active = self.store.active_generation()
        generation = None if active is None else active["generation"]
        self._journal(activation_id, "published", "applied",
                      {"provider_id": provider_id, "affected_consumers": affected})
        receipt = self._receipt(
            activation_id, idempotency_key, "shutdown", previous=generation, new=generation,
            plan_digest=(plan or {}).get("digest") or digest({}),
            registrations={"added": [], "removed": [f"provider:{provider_id}"], "retained": []},
            stages=[{"stage": "published", "status": "applied", "detail": "provider shut down"}],
            status="applied", recovery="not-needed", principal_id=principal_id)
        return {**self._finish(activation_id, receipt), "affected_consumers": affected}

    def restore_provider(self, provider_id: str, *, principal_id: str) -> None:
        self.store.set_provider_state(provider_id, "active", reason="restored", principal_id=principal_id)

    def uninstall(self, name: str, version: str, idempotency_key: str, *, principal_id: str) -> dict[str, Any]:
        """Remove an unused manifest; refuse anything selected, active or pinned.

        Evidence and records are never touched here; data deletion is a
        separate retention operation.
        """

        activation_id, prior = self._begin(idempotency_key, "uninstall", {"name": name, "version": version})
        if prior is not None:
            return prior
        in_use = []
        if name in self.store.selection():
            in_use.append("selected")
        active = self.store.active_plan()
        if active and any(m["name"] == name and m["version"] == version for m in active["manifests"]):
            in_use.append("active-generation")
        if any(any(m["name"] == name and m["version"] == version for m in p["manifests"])
               for p in self.store.pinned_plans()):
            in_use.append("pinned-run")
        if in_use:
            raise CompositionError("in_use", f"{name}@{version} is still needed: {', '.join(in_use)}",
                                   reasons=in_use)
        self.store.remove_manifest(name, version)
        generation = self.store.active_generation()
        gen = None if generation is None else generation["generation"]
        self._journal(activation_id, "published", "applied", {"uninstalled": f"{name}@{version}"})
        receipt = self._receipt(
            activation_id, idempotency_key, "uninstall", previous=gen, new=gen,
            plan_digest=(active or {}).get("digest") or digest({}),
            registrations={"added": [], "removed": [f"manifest:{name}@{version}"], "retained": []},
            stages=[{"stage": "published", "status": "applied"}], status="applied",
            recovery="not-needed", principal_id=principal_id)
        return self._finish(activation_id, receipt)

    # -- C05.5 authority -----------------------------------------------------
    def cutover(self, bundle: str, idempotency_key: str, *, principal_id: str) -> dict[str, Any]:
        """Hand a bundle's lifecycle to the coordinator (one authority).

        The bundle must be installed in the composition store; it need not be
        selected. An installed but unselected bundle is cut over disabled and
        stays so until it is selected through the coordinator.
        """

        plan = self.store.active_plan()
        if bundle not in {m["name"] for m in self.store.manifests()}:
            raise CompositionError("not_installed", f"{bundle} is not installed in the composition store")
        activation_id, prior = self._begin(idempotency_key, "cutover", {"bundle": bundle})
        if prior is not None:
            return prior
        from src.domains import pack_install

        pack_install._INSTALLED.pop(bundle, None)  # the legacy ledger stops describing it
        self.store.set_authority(bundle, "composition", principal_id=principal_id)
        changes = self.apply_active()
        if bundle not in runtime().bundles:
            # Not selected: whatever the legacy path had enabled stops serving.
            _remove_registration(bundle)
            changes["removed"] = sorted({*changes["removed"], f"bundle:{bundle}@legacy"})
        install_authority(self)
        return self._authority_receipt(activation_id, idempotency_key, "cutover", plan, changes, principal_id)

    def rollback(self, bundle: str, idempotency_key: str, *, principal_id: str) -> dict[str, Any]:
        """Compatibility-flag rollback: restore legacy registrations for one bundle."""

        activation_id, prior = self._begin(idempotency_key, "rollback", {"bundle": bundle})
        if prior is not None:
            return prior
        plan = self.store.active_plan()
        self.store.set_authority(bundle, "legacy", principal_id=principal_id, rolled_back=True)
        changes = self.apply_active()
        manifests = {m["name"]: m for m in self.store.manifests() if m["name"] == bundle}
        pinned = next((m for m in (plan or {}).get("manifests", []) if m["name"] == bundle), None)
        manifest = None
        if pinned is not None:
            manifest = next((m for m in self.store.manifests()
                             if m["name"] == bundle and m["version"] == pinned["version"]), None)
        manifest = manifest or manifests.get(bundle)
        if manifest is not None and manifest.get("adapter", {}).get("source") != "domain-pack":
            from src.domains import pack_install
            from src.domains.pack_format import PackManifest

            pack_install.install_manifest(PackManifest.from_dict(v1_view(manifest)))
        elif manifest is not None:
            from src.domains import registry as domain_registry

            domain_registry._set_enabled(bundle, True)
        self._journal(activation_id, "rolled-back", "applied", {"bundle": bundle})
        return self._authority_receipt(activation_id, idempotency_key, "rollback", plan, changes,
                                       principal_id, stage="rolled-back")

    def _authority_receipt(self, activation_id, key, operation, plan, changes, principal_id, stage="published"):
        active = self.store.active_generation()
        gen = None if active is None else active["generation"]
        if stage == "published":
            self._journal(activation_id, "published", "applied", {"operation": operation})
        receipt = self._receipt(
            activation_id, key, operation, previous=gen, new=gen,
            plan_digest=(plan or {}).get("digest") or digest({}), registrations=changes,
            stages=[{"stage": stage, "status": "applied"}], status="applied",
            recovery="not-needed", principal_id=principal_id)
        return self._finish(activation_id, receipt)

    def authority_hook(self, name: str, operation: str) -> bool:
        """Legacy registry/installer hook: delegate, refuse, or leave to legacy."""

        if name not in self.managed():
            return False
        if operation == "query":
            return True
        from src.domains.registry import CompatibilityError

        if operation == "enable":
            if name not in self.store.selection():
                plan = self.store.active_plan() or {}
                pin = next((m for m in plan.get("manifests", []) if m["name"] == name), None)
                version = pin["version"] if pin else max(
                    (m["version"] for m in self.store.manifests() if m["name"] == name),
                    key=lambda v: tuple(int(x) for x in v.split(".")))
                self.store.select(name, version, principal_id="legacy-delegation")
                self.activate(f"legacy-enable:{name}:{self.now()}", principal_id="legacy-delegation")
            return True
        if operation == "disable":
            if name in self.store.selection():
                self.disable(name, f"legacy-disable:{name}:{self.now()}", principal_id="legacy-delegation")
            return True
        raise CompatibilityError(
            name, operation,
            f"{name} is composition-managed; {operation} through the composition coordinator "
            "(delegating would change install/uninstall semantics)",
        )

    # -- readiness ------------------------------------------------------------
    def visible_consumers(self, principal_id: str | None) -> set[str]:
        plan = self.store.active_plan() or {}
        roots = self.store.visible_roots(principal_id)
        selected = set(self.store.selection())
        return {f"{m['name']}@{m['version']}" for m in plan.get("manifests", [])
                if m["name"] in roots or m["name"] not in selected}

    def readiness(self, *, principal_id: str | None, scopes: Iterable[str], **context: Any) -> dict[str, Any]:
        from src.composition.readiness import assess

        plan = self.store.active_plan()
        if plan is None:
            raise CompositionError("no_active_generation", "no composition generation is active")
        from src.composition.sources import exhausted_providers

        providers = self.store.providers()
        conn = context.pop("conn", self.conn)
        context.setdefault("exhausted_accounts", exhausted_providers(conn, providers, now=self.now))
        return assess(plan, providers, conn=conn, scopes=scopes,
                      shutdown_providers=self.shutdown_providers(),
                      visible_consumers=self.visible_consumers(principal_id), **context)


def _changes(active: Mapping[str, Any] | None, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Closure and affected consumers between the active plan and a candidate plan."""

    before = {(b["consumer"], b["capability"]): (b["provider_id"], b["provider_version"])
              for b in (active or {}).get("bindings", [])}
    after = {(b["consumer"], b["capability"]): (b["provider_id"], b["provider_version"])
             for b in plan["bindings"]}
    affected = sorted({key[0] for key in set(before) | set(after) if before.get(key) != after.get(key)})
    old_packs = {f"{m['name']}@{m['version']}" for m in (active or {}).get("manifests", [])}
    new_packs = {f"{m['name']}@{m['version']}" for m in plan["manifests"]}
    return {
        "closure": sorted(new_packs),
        "added": sorted(new_packs - old_packs),
        "removed": sorted(old_packs - new_packs),
        "affected_consumers": affected,
        "binding_changes": [
            {"consumer": key[0], "capability": key[1],
             "before": None if before.get(key) is None else "@".join(before[key]),
             "after": None if after.get(key) is None else "@".join(after[key])}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)
        ],
    }


_INSTALLED_COORDINATOR: Coordinator | None = None


def installed_coordinator() -> Coordinator | None:
    """The coordinator currently acting as the legacy registry's authority."""

    return _INSTALLED_COORDINATOR


def install_authority(coordinator: Coordinator | None) -> None:
    """Install the coordinator as the legacy registry's authority hook."""

    global _INSTALLED_COORDINATOR
    from src.domains import registry as domain_registry

    if coordinator is None or legacy_mode():
        _INSTALLED_COORDINATOR = None
        domain_registry.set_authority(None)
        return
    _INSTALLED_COORDINATOR = coordinator
    domain_registry.set_authority(coordinator.authority_hook)


def startup(conn: Any) -> dict[str, Any] | None:
    """Process-startup entry point (ADR-003): reconcile when composition state exists."""

    from src.composition.store import initialized

    if conn is None or not initialized(conn):
        return None
    return Coordinator(conn).reconcile()


__all__ = [
    "LEGACY_FLAG",
    "STAGES",
    "Coordinator",
    "Crash",
    "Runtime",
    "install_authority",
    "installed_coordinator",
    "legacy_mode",
    "reset_runtime",
    "runtime",
    "startup",
]
