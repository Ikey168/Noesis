"""Durable composition lifecycle (C05): selections, journal, generations, coordinator.

Storage follows ADR-003: composition-metadata tables on the existing DuckDB
warehouse connection, immutable rows plus one active-generation pointer
switched inside a transaction, and an append-only journal. Nothing here is a
second database, scheduler or enabled-state ledger.

States are distinct and persisted separately:

* **installed** — an immutable manifest or provider descriptor retained by
  content hash (``install``);
* **selected** — deployment intent for a root: range, optional features,
  configured provider choices (``select``);
* **resolved** — a plan stored by digest and linked to the selection that
  produced it (``resolve``);
* **published** — a generation the active pointer names (``activate``).

None of these transitions makes a provider request, runs an acquisition,
touches schedules or grants scopes. Readiness is observed separately (C04)
and never stored here.

Python registrations are not atomic with the pointer switch and are not
claimed to be: they are *staged* before publish (the old registration keeps
serving), swapped in place after the switch, and rebuilt from the active
generation by :meth:`CompositionCoordinator.reconcile` at startup.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.composition.contracts import (
    MANIFEST_CONTRACT,
    RECEIPT_CONTRACT,
    canonical_json,
    content_hash,
    validate_composition_manifest,
    validate_plan,
    validate_provider_descriptor,
    validate_receipt,
)
from src.composition.readiness import CompositionView, assess
from src.composition.resolver import Resolution, resolve

STAGES = ("previewed", "staged", "verified", "published")
_RECEIPT_STAGE = {"previewed": "preview", "staged": "stage", "verified": "verify", "published": "publish"}

_DDL = """
CREATE TABLE IF NOT EXISTS composition_installed(
 kind TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
 document_json TEXT NOT NULL, installed_at_ms BIGINT NOT NULL, PRIMARY KEY(kind, id, version, content_hash));
CREATE TABLE IF NOT EXISTS composition_selections(
 root TEXT PRIMARY KEY, spec TEXT NOT NULL, features_json TEXT, providers_json TEXT NOT NULL,
 updated_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_plans(
 digest TEXT PRIMARY KEY, plan_json TEXT NOT NULL, selection_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_generations(
 generation_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, previous_generation TEXT,
 retained_providers_json TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_active(
 slot INTEGER PRIMARY KEY, generation_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_journal(
 seq BIGINT PRIMARY KEY, activation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, stage TEXT NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_authority(
 bundle TEXT PRIMARY KEY, authority TEXT NOT NULL, compat_rollback BOOLEAN NOT NULL, updated_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_provider_admin(
 provider_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, principal TEXT NOT NULL,
 updated_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS composition_run_pins(
 run_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, pinned_at_ms BIGINT NOT NULL);
"""


class CompositionLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class SimulatedCrash(RuntimeError):
    """Test hook: the process dies after the named journal stage is durable."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _short_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def registry_name(manifest: Mapping[str, Any]) -> str:
    """The name the process-local domain registry uses for a bundle.

    Adapted bundles keep their original v1/code name (``research`` for the
    ``science`` bundle) so existing registrations and config keep working.
    """

    names = list((manifest.get("adapter") or {}).get("legacy_names") or [])
    others = [n for n in names if n != manifest["id"]]
    return others[0] if others else str(manifest["id"])


class CompositionCoordinator:
    """Preview, stage, verify and publish composition generations (C05.3)."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, registry: Any = None,
                 legacy_config: Callable[[], Iterable[str]] | None = None) -> None:
        from src.domains import registry as domain_registry

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.registry = registry or domain_registry
        self.legacy_config = legacy_config or _legacy_enabled_names
        self._synthesized: set[str] = set()
        for statement in filter(str.strip, _DDL.split(";")):
            conn.execute(statement)

    # ------------------------------------------------------------ C05.1 installed / selected / resolved

    def install(self, document: Mapping[str, Any]) -> dict[str, str]:
        """Retain an immutable manifest or provider descriptor by content hash."""

        if document.get("pack_format") == MANIFEST_CONTRACT:
            kind, issues = "pack", validate_composition_manifest(document)
        elif document.get("contract") == "noesis-provider-descriptor-v1":
            kind, issues = "provider", validate_provider_descriptor(document)
        else:
            raise CompositionLifecycleError("unknown_document", "not a composition manifest or provider descriptor")
        if issues:
            raise CompositionLifecycleError("invalid_document", issues[0].message, issues=[i.as_dict() for i in issues])
        digest = str(document.get("content_hash") or content_hash(document))
        self.conn.execute("INSERT INTO composition_installed VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [kind, document["id"], document["version"], digest, _json(document), self.now()])
        return {"kind": kind, "id": str(document["id"]), "version": str(document["version"]), "content_hash": digest}

    def installed(self, kind: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT document_json FROM composition_installed WHERE kind=? "
                                 "ORDER BY id, version, content_hash", [kind]).fetchall()
        return [json.loads(row[0]) for row in rows]

    def select(self, root: str, spec: str, *, features: Iterable[str] | None = None,
               providers: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Record deployment intent for one root. Implies nothing about readiness."""

        record = {"root": root, "spec": spec, "features": sorted(features) if features is not None else None,
                  "providers": dict(sorted((providers or {}).items()))}
        self.conn.execute(
            "INSERT INTO composition_selections VALUES (?,?,?,?,?) ON CONFLICT (root) DO UPDATE SET "
            "spec=excluded.spec, features_json=excluded.features_json, providers_json=excluded.providers_json, "
            "updated_at_ms=excluded.updated_at_ms",
            [root, spec, None if features is None else _json(record["features"]), _json(record["providers"]),
             self.now()])
        return record

    def deselect(self, root: str) -> bool:
        found = self.conn.execute("SELECT 1 FROM composition_selections WHERE root=?", [root]).fetchone()
        self.conn.execute("DELETE FROM composition_selections WHERE root=?", [root])
        return bool(found)

    def selections(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT root, spec, features_json, providers_json FROM composition_selections "
                                 "ORDER BY root").fetchall()
        return [{"root": r, "spec": s, "features": None if f is None else json.loads(f), "providers": json.loads(p)}
                for r, s, f, p in rows]

    def _selection_hash(self, selections: list[dict[str, Any]]) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(selections).encode()).hexdigest()

    def resolve(self, *, upgrade: Iterable[str] = (), selections: list[dict[str, Any]] | None = None,
                persist: bool = True) -> Resolution:
        """Resolve the current selections over the installed candidates.

        The active generation's plan is the retained plan, so compatible pins
        hold unless named in ``upgrade``. A successful plan is stored by digest.
        """

        selections = self.selections() if selections is None else selections
        if not selections:
            raise CompositionLifecycleError("no_selection", "no root is selected")
        roots = [{"pack": s["root"], "range": s["spec"],
                  **({"features": s["features"]} if s["features"] is not None else {})} for s in selections]
        choices: dict[str, str] = {}
        for selection in selections:
            choices.update(selection["providers"])
        active = self.active()
        result = resolve(roots, self.installed("pack"), self.installed("provider"), selections=choices,
                         retained=active["plan"] if active else None, upgrade=upgrade)
        if result.ok and persist:
            self.conn.execute("INSERT INTO composition_plans VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                              [result.plan["digest"], _json(result.plan), self._selection_hash(selections),
                               self.now()])
        return result

    def plan(self, digest: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT plan_json FROM composition_plans WHERE digest=?", [digest]).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------ generations

    def active(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT g.generation_id, g.plan_digest, g.retained_providers_json, g.receipt_json FROM composition_active a "
            "JOIN composition_generations g ON g.generation_id=a.generation_id WHERE a.slot=1").fetchone()
        if not row:
            return None
        return {"id": row[0], "plan_digest": row[1], "plan": self.plan(row[1]),
                "retained_providers": json.loads(row[2]), "receipt": json.loads(row[3])}

    def generation(self, generation_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT plan_digest, receipt_json FROM composition_generations WHERE generation_id=?",
                                [generation_id]).fetchone()
        return {"id": generation_id, "plan_digest": row[0], "plan": self.plan(row[0]),
                "receipt": json.loads(row[1])} if row else None

    def view(self, generation: Mapping[str, Any] | None = None) -> CompositionView | None:
        """The catalog's view of the active (or a named) generation (C04)."""

        generation = generation or self.active()
        if not generation:
            return None
        return CompositionView(generation["plan"], self.installed("provider"), self.installed("pack"))

    # ------------------------------------------------------------ journal

    def _journal(self, activation_id: str, key: str, stage: str, status: str, payload: Any = None) -> None:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM composition_journal").fetchone()[0]
        self.conn.execute("INSERT INTO composition_journal VALUES (?,?,?,?,?,?,?)",
                          [seq, activation_id, key, stage, status, _json(payload or {}), self.now()])

    def journal(self, activation_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT seq, activation_id, idempotency_key, stage, status, payload_json FROM composition_journal"
        rows = self.conn.execute(sql + (" WHERE activation_id=?" if activation_id else "") + " ORDER BY seq",
                                 [activation_id] if activation_id else []).fetchall()
        return [{"seq": s, "activation_id": a, "idempotency_key": k, "stage": st, "status": status,
                 "payload": json.loads(p)} for s, a, k, st, status, p in rows]

    # ------------------------------------------------------------ C05.3 preview / stage / verify / publish

    def preview(self, *, upgrade: Iterable[str] = (), selections: list[dict[str, Any]] | None = None
                ) -> dict[str, Any]:
        """Closure and affected consumers of the proposed change against the active generation."""

        result = self.resolve(upgrade=upgrade, selections=selections, persist=False)
        active = self.active()
        before = (active or {}).get("plan") or {}
        if not result.ok:
            return {"ok": False, "failure": result.failure.as_dict()}
        return {"ok": True, **_diff(before, result.plan), "plan_digest": result.plan["digest"],
                "previous_plan_digest": before.get("digest")}

    def activate(self, idempotency_key: str, *, upgrade: Iterable[str] = (), crash_after: str | None = None,
                 fail_verify: Callable[[dict[str, Any]], str | None] | None = None,
                 source_upgrades: Iterable[Mapping[str, str]] = ()) -> dict[str, Any]:
        """Preview, stage, verify, then publish one generation; return its receipt.

        Idempotent per key: a key that already published returns its receipt.
        ``source_upgrades`` names owner operations (source-pack upgrade
        ``pack_id``/``apply_key``/``principal_id``) this activation staged;
        they are journaled and reconciled from owner receipts, never re-run.
        """

        existing = self._published_receipt(idempotency_key)
        if existing is not None:
            return existing
        activation_id = "activation:" + _short_hash([idempotency_key, self.now()])
        previous = self.active()
        previous_ref = {"id": previous["id"], "plan_digest": previous["plan_digest"]} if previous else None
        stages: list[dict[str, str]] = []

        def fail(stage: str, detail: str, new_generation: dict[str, str] | None) -> dict[str, Any]:
            self._journal(activation_id, idempotency_key, "failed", "failed", {"stage": stage, "detail": detail})
            stages.append({"stage": _RECEIPT_STAGE[stage], "status": "failed", "detail": detail[:500]})
            return self._receipt(activation_id, idempotency_key, "failed", previous_ref, new_generation, [], stages,
                                 [], "none")

        def mark(stage: str, payload: Any = None) -> None:
            self._journal(activation_id, idempotency_key, stage, "applied" if stage != "staged" else "staged",
                          payload)
            stages.append({"stage": _RECEIPT_STAGE[stage], "status": "applied"})
            if crash_after == stage:
                raise SimulatedCrash(stage)

        # preview
        result = self.resolve(upgrade=upgrade)
        if not result.ok:
            return fail("previewed", f"{result.failure.code}: {result.failure.message}", None)
        plan = result.plan
        generation_id = "generation:" + _short_hash([plan["digest"], activation_id])
        new_ref = {"id": generation_id, "plan_digest": plan["digest"]}
        diff = _diff((previous or {}).get("plan") or {}, plan)
        mark("previewed", {"plan_digest": plan["digest"], "affected_consumers": diff["affected_consumers"]})

        # stage: build registrations for the new generation without touching the live registry
        staged = self._stage(plan)
        upgrades = [dict(u) for u in source_upgrades]
        mark("staged", {"packs": sorted(staged), "source_upgrades": upgrades})

        # verify: staged registrations match the plan; readiness is observed, never gating or stored
        problems = validate_plan(plan)
        missing = sorted({p["id"] for p in plan["packs"]} - {m for _, m in staged.values()})
        detail = (problems[0].message if problems else f"unstaged packs {missing}" if missing else
                  fail_verify(plan) if fail_verify else None)
        if detail:
            return fail("verified", detail, new_ref)
        observation = assess(CompositionView(plan, self.installed("provider"), self.installed("pack")),
                             conn=self.conn, now_ms=self.now)
        mark("verified", {"readiness_operations": len(observation["operations"])})

        # publish: one transaction inserts the generation, journals publish and switches the pointer
        retained = self._retained_providers(plan)
        affected = self._affected(previous, plan, staged, retained)
        receipt = self._receipt(activation_id, idempotency_key, "published", previous_ref, new_ref, affected,
                                stages + [{"stage": "publish", "status": "applied"}], upgrades, "none")
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("INSERT INTO composition_plans VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                              [plan["digest"], _json(plan), self._selection_hash(self.selections()), self.now()])
            self.conn.execute("INSERT INTO composition_generations VALUES (?,?,?,?,?,?)",
                              [generation_id, plan["digest"], previous_ref and previous_ref["id"],
                               _json(retained), _json(receipt), self.now()])
            self._journal(activation_id, idempotency_key, "published", "applied", new_ref)
            self.conn.execute("INSERT INTO composition_active VALUES (1,?,?) ON CONFLICT (slot) DO UPDATE SET "
                              "generation_id=excluded.generation_id, updated_at_ms=excluded.updated_at_ms",
                              [generation_id, self.now()])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if crash_after == "published":
            raise SimulatedCrash("published")
        self._apply(staged, (previous or {}).get("plan"))
        return receipt

    def _published_receipt(self, key: str) -> dict[str, Any] | None:
        for row in self.conn.execute("SELECT receipt_json FROM composition_generations ORDER BY created_at_ms"
                                     ).fetchall():
            receipt = json.loads(row[0])
            if receipt["idempotency_key"] == key:
                return receipt
        return None

    def _receipt(self, activation_id: str, key: str, status: str, previous: dict[str, str] | None,
                 new: dict[str, str] | None, affected: list[dict[str, str]], stages: list[dict[str, str]],
                 upgrades: list[dict[str, str]], recovery: str) -> dict[str, Any]:
        receipt = {"contract": RECEIPT_CONTRACT, "receipt_id": activation_id, "idempotency_key": key,
                   "status": status, "previous_generation": previous, "new_generation": new,
                   "affected_registrations": affected,
                   "source_upgrade_receipts": [{"pack_id": u["pack_id"], "apply_key": u["apply_key"]}
                                               for u in upgrades],
                   "stages": stages, "recovery_status": recovery, "created_at_ms": self.now()}
        issues = validate_receipt(receipt)
        if issues:
            raise CompositionLifecycleError("invalid_receipt", issues[0].message)
        return receipt

    def _stage(self, plan: Mapping[str, Any]) -> dict[str, tuple[Any, str]]:
        """Registration objects for every composition-managed pack in the plan, keyed by registry name."""

        manifests = {(m["id"], m["version"]): m for m in self.installed("pack")}
        staged: dict[str, tuple[Any, str]] = {}
        for pin in plan["packs"]:
            manifest = manifests.get((pin["id"], pin["version"]))
            if manifest is None:
                continue
            name = registry_name(manifest)
            existing = self.registry.get_pack(name)
            if existing is not None and name not in self._synthesized:
                staged[name] = (existing, pin["id"])  # code registrations are kept, never reloaded
            else:
                staged[name] = (_synthesize(name, manifest), pin["id"])
        return staged

    def _apply(self, staged: Mapping[str, tuple[Any, str]], previous_plan: Mapping[str, Any] | None) -> None:
        """Swap staged registrations in place; the previous object serves until replaced."""

        for name, (pack, bundle) in sorted(staged.items()):
            if not self.is_composition_managed(bundle):
                continue
            if name not in self._synthesized and self.registry.get_pack(name) is pack:
                pass
            else:
                self.registry.register_pack(pack)
                self._synthesized.add(name)
            self.registry.apply_enabled(name, True)
        keep = {name for name in staged}
        for pin in (previous_plan or {}).get("packs") or []:
            name = self._name_for(pin["id"], pin["version"])
            if name not in keep and self.is_composition_managed(pin["id"]):
                self.registry.apply_enabled(name, False)

    def _name_for(self, pack_id: str, version: str) -> str:
        for manifest in self.installed("pack"):
            if manifest["id"] == pack_id and manifest["version"] == version:
                return registry_name(manifest)
        return pack_id

    def _affected(self, previous: Mapping[str, Any] | None, plan: Mapping[str, Any],
                  staged: Mapping[str, tuple[Any, str]], retained: list[str]) -> list[dict[str, str]]:
        before = {p["id"]: p["version"] for p in ((previous or {}).get("plan") or {}).get("packs") or []}
        after = {p["id"]: p["version"] for p in plan["packs"]}
        affected = []
        for pack_id in sorted(set(before) | set(after)):
            if pack_id not in before:
                affected.append({"kind": "pack", "id": pack_id, "action": "register"})
            elif pack_id not in after:
                affected.append({"kind": "pack", "id": pack_id, "action": "unregister"})
                affected.append({"kind": "catalog", "id": pack_id, "action": "unregister"})
            elif before[pack_id] != after[pack_id]:
                affected.append({"kind": "pack", "id": pack_id, "action": "replace"})
        old = {b["capability"]: b for b in ((previous or {}).get("plan") or {}).get("bindings") or []}
        for binding in plan["bindings"]:
            prior = old.pop(binding["capability"], None)
            if prior is None:
                affected.append({"kind": "binding", "id": binding["capability"], "action": "register"})
            elif (prior["provider"], prior["provider_version"]) != (binding["provider"], binding["provider_version"]):
                affected.append({"kind": "binding", "id": binding["capability"], "action": "replace"})
        affected += [{"kind": "binding", "id": c, "action": "unregister"} for c in sorted(old)]
        affected += [{"kind": "binding", "id": p, "action": "retain"} for p in retained]
        return affected

    # ------------------------------------------------------------ C05.2 startup reconciliation

    def reconcile(self) -> dict[str, Any]:
        """Rebuild bindings from the active generation and settle interrupted journal entries.

        Idempotent. Never issues provider requests, acquisitions, source runs,
        schedule changes or a new publication.
        """

        active = self.active()
        rebuilt: list[str] = []
        if active:
            staged = self._stage(active["plan"])
            self._apply(staged, None)
            rebuilt = sorted(name for name, (_, bundle) in staged.items() if self.is_composition_managed(bundle))
        settled = []
        by_activation: dict[str, list[dict[str, Any]]] = {}
        for entry in self.journal():
            by_activation.setdefault(entry["activation_id"], []).append(entry)
        for activation_id, entries in sorted(by_activation.items()):
            stages = {e["stage"] for e in entries}
            key = entries[0]["idempotency_key"]
            if not stages & {"published", "failed", "recovered"}:
                self._journal(activation_id, key, "recovered", "failed",
                              {"detail": "interrupted before publish; the previous generation stayed active"})
                settled.append({"activation_id": activation_id, "status": "failed"})
            for entry in entries:
                if entry["stage"] != "staged":
                    continue
                for upgrade in entry["payload"].get("source_upgrades") or []:
                    marker = f"source-upgrade:{upgrade['pack_id']}:{upgrade['apply_key']}"
                    if any(e["stage"] == marker for e in entries):
                        continue
                    status = self._source_upgrade_status(upgrade)
                    self._journal(activation_id, key, marker, status, upgrade)
                    settled.append({"activation_id": activation_id, "owner_operation": marker, "status": status})
        return {"active_generation": active and active["id"], "plan_digest": active and active["plan_digest"],
                "rebuilt_registrations": rebuilt, "bindings": (active or {}).get("plan", {}).get("bindings", []),
                "settled": settled}

    def _source_upgrade_status(self, upgrade: Mapping[str, str]) -> str:
        """Read the source-pack owner receipt; never re-run the upgrade."""

        try:
            row = self.conn.execute(
                "SELECT receipt_json FROM source_pack_upgrade_receipts WHERE pack_id=? AND apply_key=? "
                "AND principal_id=?", [upgrade["pack_id"], upgrade["apply_key"], upgrade.get("principal_id", "")]
            ).fetchone()
        except Exception:  # noqa: BLE001 - the owner table may not exist yet
            return "unknown"
        return "applied" if row else "unknown"

    # ------------------------------------------------------------ C05.4 disable, shutdown, uninstall, run pins

    def pin_run(self, run_id: str, generation_id: str | None = None) -> None:
        generation_id = generation_id or (self.active() or {}).get("id")
        if not generation_id:
            raise CompositionLifecycleError("no_generation", "no active generation to pin")
        self.conn.execute("INSERT INTO composition_run_pins VALUES (?,?,?) ON CONFLICT DO NOTHING",
                          [run_id, generation_id, self.now()])

    def release_run(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM composition_run_pins WHERE run_id=?", [run_id])

    def _pinned_providers(self) -> set[str]:
        pinned: set[str] = set()
        for (generation_id,) in self.conn.execute("SELECT generation_id FROM composition_run_pins").fetchall():
            generation = self.generation(generation_id)
            if generation and generation["plan"]:
                pinned |= {p["id"] for p in generation["plan"]["providers"]}
        return pinned

    def _retained_providers(self, plan: Mapping[str, Any]) -> list[str]:
        return sorted(self._pinned_providers() - {p["id"] for p in plan["providers"]})

    def disable(self, root: str, idempotency_key: str) -> dict[str, Any]:
        """Remove a root's selection and publish the recomputed closure.

        Providers another selected root still needs stay bound; providers
        only a pinned active run needs are retained (receipt action
        ``retain``). The root's catalog entry points are withdrawn with its
        registration; evidence and source versions are untouched.
        """

        if not self.deselect(root):
            raise CompositionLifecycleError("not_selected", f"{root!r} is not selected")
        if not self.selections():
            return self._publish_empty(idempotency_key)
        return self.activate(idempotency_key)

    def _publish_empty(self, key: str) -> dict[str, Any]:
        previous = self.active()
        activation_id = "activation:" + _short_hash([key, self.now()])
        self._journal(activation_id, key, "failed", "failed", {"detail": "no remaining selection"})
        if previous:
            for pin in previous["plan"]["packs"]:
                if self.is_composition_managed(pin["id"]):
                    self.registry.apply_enabled(self._name_for(pin["id"], pin["version"]), False)
            self.conn.execute("DELETE FROM composition_active WHERE slot=1")
        previous_ref = {"id": previous["id"], "plan_digest": previous["plan_digest"]} if previous else None
        return self._receipt(activation_id, key, "rolled-back", previous_ref, None,
                             [{"kind": "pack", "id": p["id"], "action": "unregister"}
                              for p in ((previous or {}).get("plan") or {}).get("packs", [])],
                             [{"stage": "publish", "status": "skipped", "detail": "no selected root remains"}], [],
                             "none")

    def shutdown_provider(self, provider_id: str, *, reason: str, principal: str) -> dict[str, Any]:
        """Administrative shutdown: an explicit action with its consequences listed."""

        self.conn.execute(
            "INSERT INTO composition_provider_admin VALUES (?,?,?,?,?) ON CONFLICT (provider_id) DO UPDATE SET "
            "state=excluded.state, reason=excluded.reason, principal=excluded.principal, "
            "updated_at_ms=excluded.updated_at_ms", [provider_id, "shutdown", reason, principal, self.now()])
        plan = (self.active() or {}).get("plan") or {}
        bindings = [b for b in plan.get("bindings") or [] if b["provider"] == provider_id]
        return {"provider": provider_id, "state": "shutdown", "reason": reason,
                "affected_consumers": sorted({c for b in bindings for c in b["consumers"]}),
                "unavailable_operations": sorted(f"{b['capability']}:{op}" for b in bindings for op in b["operations"])}

    def restore_provider(self, provider_id: str) -> None:
        self.conn.execute("DELETE FROM composition_provider_admin WHERE provider_id=?", [provider_id])

    def shutdowns(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT provider_id, reason FROM composition_provider_admin "
                                      "WHERE state='shutdown'").fetchall())

    def readiness(self, **options: Any) -> dict[str, Any] | None:
        """Observe readiness of the active generation, honouring administrative shutdowns."""

        view = self.view()
        if view is None:
            return None
        return assess(view, conn=self.conn, shutdown_providers=self.shutdowns(), now_ms=self.now, **options)

    def uninstall(self, kind: str, item_id: str, version: str | None = None) -> dict[str, Any]:
        """Remove unused installed documents and their registrations.

        Refused while the active generation or a pinned run references the
        item. Never deletes records, evidence, source versions or cursors: data
        deletion is a separate retention operation.
        """

        active_plan = (self.active() or {}).get("plan") or {}
        in_use = {(p["id"], p["version"]) for p in active_plan.get(f"{kind}s" if kind == "provider" else "packs", [])}
        pinned_generations = [self.generation(g) for (g,) in
                              self.conn.execute("SELECT generation_id FROM composition_run_pins").fetchall()]
        for generation in pinned_generations:
            plan = (generation or {}).get("plan") or {}
            in_use |= {(p["id"], p["version"]) for p in plan.get("providers" if kind == "provider" else "packs", [])}
        rows = self.conn.execute("SELECT version, document_json FROM composition_installed WHERE kind=? AND id=?",
                                 [kind, item_id]).fetchall()
        targets = [(v, json.loads(d)) for v, d in rows if version is None or v == version]
        blocked = sorted(v for v, _ in targets if (item_id, v) in in_use)
        if blocked:
            raise CompositionLifecycleError("in_use", f"{kind} {item_id} {blocked} is referenced by the active "
                                            "generation or a pinned run", versions=blocked)
        for v, document in targets:
            self.conn.execute("DELETE FROM composition_installed WHERE kind=? AND id=? AND version=?",
                              [kind, item_id, v])
            if kind == "pack":
                name = registry_name(document)
                if name in self._synthesized:
                    self.registry.apply_enabled(name, False)
                    self.registry._REGISTRY.pop(name, None)
                    self._synthesized.discard(name)
        return {"kind": kind, "id": item_id, "removed_versions": sorted(v for v, _ in targets),
                "data_deleted": False}

    # ------------------------------------------------------------ C05.5 authority

    def cutover(self, bundle: str) -> None:
        """Make the coordinator the only authority for a bundle's enablement."""

        self._set_authority(bundle, "composition", False)
        self.registry.set_authority(self)

    def rollback_to_legacy(self, bundle: str, idempotency_key: str) -> dict[str, Any]:
        """Compatibility flag: hand the bundle back to the legacy path.

        Restores legacy enablement from the legacy configuration. Retained
        source versions, cursors, records and generations are left as they are.
        """

        self._set_authority(bundle, "legacy", True)
        names = self._legacy_names(bundle)
        configured = set(self.legacy_config())
        for name in names:
            self.registry.apply_enabled(name, name in configured)
            self._synthesized.discard(name)
        activation_id = "rollback:" + _short_hash([bundle, idempotency_key])
        self._journal(activation_id, idempotency_key, "compat-rollback", "applied", {"bundle": bundle,
                                                                                    "names": sorted(names)})
        return {"bundle": bundle, "authority": "legacy", "compat_rollback": True,
                "enabled": sorted(n for n in names if n in configured)}

    def _set_authority(self, bundle: str, authority: str, rollback: bool) -> None:
        self.conn.execute(
            "INSERT INTO composition_authority VALUES (?,?,?,?) ON CONFLICT (bundle) DO UPDATE SET "
            "authority=excluded.authority, compat_rollback=excluded.compat_rollback, "
            "updated_at_ms=excluded.updated_at_ms", [bundle, authority, rollback, self.now()])

    def is_composition_managed(self, bundle: str) -> bool:
        from src.composition.adapter import bundle_id

        row = self.conn.execute("SELECT authority FROM composition_authority WHERE bundle=?",
                                [bundle_id(bundle)]).fetchone()
        return bool(row and row[0] == "composition")

    def _legacy_names(self, bundle: str) -> set[str]:
        from src.composition.adapter import LEGACY_ALIASES

        return {bundle} | {alias for alias, target in LEGACY_ALIASES.items() if target == bundle}

    # registry authority protocol
    def manages(self, name: str) -> bool:
        return self.is_composition_managed(name)

    def _active_names(self) -> set[str]:
        plan = (self.active() or {}).get("plan") or {}
        return {self._name_for(p["id"], p["version"]) for p in plan.get("packs", [])}

    def legacy_enable(self, name: str) -> None:
        from src.domains.registry import CompositionAuthorityError

        if name in self._active_names():
            return  # already the state the coordinator publishes: delegate as a no-op
        raise CompositionAuthorityError(
            f"{name!r} is composition-managed; select it and activate a generation through the coordinator")

    def legacy_disable(self, name: str) -> None:
        from src.domains.registry import CompositionAuthorityError

        if name not in self._active_names():
            return
        raise CompositionAuthorityError(
            f"{name!r} is composition-managed; disable its root through the coordinator so shared "
            "dependencies are retained")


def _synthesize(name: str, manifest: Mapping[str, Any]) -> Any:
    """A ``DomainPack`` for a manifest-only bundle, compiled like the legacy installer."""

    from src.composition.adapter import v1_view
    from src.domains.base import DomainPack
    from src.domains.pack_install import _compile_enricher

    contributes = manifest.get("contributes") or {}
    view = v1_view(manifest)
    return DomainPack(name=name, description=str(manifest.get("description") or ""),
                      source_types=list(contributes.get("source_types") or []),
                      enrichers=[_compile_enricher(e) for e in contributes.get("enrichers") or []],
                      ui_flags=dict(contributes.get("ui_flags") or {}), capabilities=view["capabilities"],
                      schema_versions=view["schema_versions"], ontology_extensions=view["ontology_extensions"])


def _pin(binding: Mapping[str, Any] | None) -> dict[str, str] | None:
    return binding and {"provider": binding["provider"], "version": binding["provider_version"]}


def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    old = {b["capability"]: b for b in before.get("bindings") or []}
    new = {b["capability"]: b for b in after.get("bindings") or []}
    changes, affected = [], set()
    for capability in sorted(set(old) | set(new)):
        a, b = old.get(capability), new.get(capability)
        if _pin(a) != _pin(b):
            consumers = sorted(set((a or {}).get("consumers", [])) | set((b or {}).get("consumers", [])))
            changes.append({"capability": capability, "before": _pin(a), "after": _pin(b), "consumers": consumers})
            affected |= set(consumers)
    before_packs = {p["id"]: p["version"] for p in before.get("packs") or []}
    after_packs = {p["id"]: p["version"] for p in after.get("packs") or []}
    return {"added_packs": sorted(set(after_packs) - set(before_packs)),
            "removed_packs": sorted(set(before_packs) - set(after_packs)),
            "changed_packs": sorted(p for p in set(before_packs) & set(after_packs)
                                    if before_packs[p] != after_packs[p]),
            "binding_changes": changes, "affected_consumers": sorted(affected)}


def _legacy_enabled_names() -> list[str]:
    from src.mcp_host.catalog import PACK_CONFIG

    try:
        return list(json.loads(PACK_CONFIG.read_text()).get("enabled_packs") or [])
    except (OSError, ValueError):
        return ["news"]
