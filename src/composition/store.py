"""Durable composition state (C05.1, #1819; storage per ADR-003).

Installed, selected and resolved are three separate persisted transitions:

* **install** retains a validated manifest or provider descriptor and its hash;
* **select** records deployment intent for a root: range, optional features and
  configured provider choices, plus who owns the selection;
* **resolve** stores a plan by digest, linked to the selection that produced it.

None of them implies acquisition, schedules, granted scopes or a successful
workflow, and none of them touches the legacy ``DomainPack`` registry. The
tables live on the warehouse connection next to the source-pack store and hold
composition metadata only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.composition.contracts import (
    CompositionError,
    canonical,
    check_range,
    digest,
    validate_manifest,
    validate_plan,
    validate_provider,
)

DDL = """
CREATE TABLE IF NOT EXISTS composition_manifests (
  name TEXT NOT NULL, version TEXT NOT NULL, manifest_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL, installed_by TEXT NOT NULL, installed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(name, version)
);
CREATE TABLE IF NOT EXISTS composition_providers (
  provider_id TEXT NOT NULL, version TEXT NOT NULL, descriptor_hash TEXT NOT NULL,
  descriptor_json TEXT NOT NULL, installed_by TEXT NOT NULL, installed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(provider_id, version)
);
CREATE TABLE IF NOT EXISTS composition_selections (
  root_name TEXT PRIMARY KEY, version_range TEXT NOT NULL, features_json TEXT NOT NULL,
  provider_choices_json TEXT NOT NULL, owner TEXT, selected_by TEXT NOT NULL,
  selected_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_plans (
  digest TEXT PRIMARY KEY, plan_json TEXT NOT NULL, selection_hash TEXT NOT NULL,
  resolver_version TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_generations (
  generation BIGINT PRIMARY KEY, plan_digest TEXT NOT NULL, activation_id TEXT NOT NULL,
  published_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_active (
  singleton INTEGER PRIMARY KEY, generation BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_journal (
  activation_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
  detail_json TEXT NOT NULL, at_ms BIGINT NOT NULL, PRIMARY KEY(activation_id, stage)
);
CREATE TABLE IF NOT EXISTS composition_activations (
  activation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  request_hash TEXT NOT NULL, operation TEXT NOT NULL, receipt_json TEXT,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_provider_state (
  provider_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
  changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_authority (
  bundle TEXT PRIMARY KEY, authority TEXT NOT NULL, changed_by TEXT NOT NULL,
  changed_at_ms BIGINT NOT NULL, rolled_back BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_run_pins (
  run_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, owner TEXT NOT NULL,
  status TEXT NOT NULL, pinned_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_audit (
  sequence BIGINT PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL,
  principal_id TEXT NOT NULL, detail_json TEXT NOT NULL, at_ms BIGINT NOT NULL
);
"""

TABLES = (
    "composition_manifests", "composition_providers", "composition_selections",
    "composition_plans", "composition_generations", "composition_active",
    "composition_journal", "composition_activations", "composition_provider_state",
    "composition_authority", "composition_run_pins", "composition_audit",
)


def ensure_schema(conn: Any) -> None:
    conn.execute(DDL)


def initialized(conn: Any) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='composition_active'"
        ).fetchone())
    except Exception:  # noqa: BLE001 - absence of the store means legacy only
        return False


class CompositionStore:
    """Installed manifests and providers, selections, plans and their read API."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_schema(conn)

    # -- audit --------------------------------------------------------------
    def audit(self, action: str, subject: str, principal_id: str, detail: Mapping[str, Any]) -> None:
        row = self.conn.execute("SELECT coalesce(max(sequence), 0) + 1 FROM composition_audit").fetchone()
        self.conn.execute(
            "INSERT INTO composition_audit VALUES (?,?,?,?,?,?)",
            [int(row[0]), action, subject, principal_id, canonical(dict(detail)), self.now()],
        )

    # -- install ------------------------------------------------------------
    def install_manifest(self, manifest: Mapping[str, Any], *, principal_id: str,
                         known_capabilities: Iterable[str] | None = None) -> dict[str, Any]:
        value = validate_manifest(manifest, known_capabilities=known_capabilities)
        row = self.conn.execute(
            "SELECT manifest_hash FROM composition_manifests WHERE name=? AND version=?",
            [value["name"], value["version"]],
        ).fetchone()
        if row:
            if row[0] != value["manifest_hash"]:
                raise CompositionError(
                    "immutable_version",
                    f"{value['name']}@{value['version']} is installed with different content",
                )
            return {"name": value["name"], "version": value["version"],
                    "manifest_hash": value["manifest_hash"], "idempotent": True}
        self.conn.execute(
            "INSERT INTO composition_manifests VALUES (?,?,?,?,?,?)",
            [value["name"], value["version"], value["manifest_hash"], canonical(value),
             principal_id, self.now()],
        )
        self.audit("install-manifest", f"{value['name']}@{value['version']}", principal_id,
                   {"manifest_hash": value["manifest_hash"]})
        return {"name": value["name"], "version": value["version"],
                "manifest_hash": value["manifest_hash"], "idempotent": False}

    def install_provider(self, descriptor: Mapping[str, Any], *, principal_id: str,
                         **validation: Any) -> dict[str, Any]:
        value = validate_provider(descriptor, **validation)
        row = self.conn.execute(
            "SELECT descriptor_hash FROM composition_providers WHERE provider_id=? AND version=?",
            [value["provider_id"], value["version"]],
        ).fetchone()
        if row:
            if row[0] != value["descriptor_hash"]:
                raise CompositionError(
                    "immutable_version",
                    f"{value['provider_id']}@{value['version']} is installed with different content",
                )
            return {"provider_id": value["provider_id"], "version": value["version"],
                    "descriptor_hash": value["descriptor_hash"], "idempotent": True}
        self.conn.execute(
            "INSERT INTO composition_providers VALUES (?,?,?,?,?,?)",
            [value["provider_id"], value["version"], value["descriptor_hash"], canonical(value),
             principal_id, self.now()],
        )
        self.audit("install-provider", f"{value['provider_id']}@{value['version']}", principal_id,
                   {"descriptor_hash": value["descriptor_hash"]})
        return {"provider_id": value["provider_id"], "version": value["version"],
                "descriptor_hash": value["descriptor_hash"], "idempotent": False}

    def remove_manifest(self, name: str, version: str) -> None:
        self.conn.execute("DELETE FROM composition_manifests WHERE name=? AND version=?", [name, version])

    def manifests(self) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT manifest_json FROM composition_manifests ORDER BY name, version").fetchall()]

    def providers(self) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT descriptor_json FROM composition_providers ORDER BY provider_id, version").fetchall()]

    # -- select -------------------------------------------------------------
    def select(self, root: str, version_range: str, *, principal_id: str,
               features: Iterable[str] = (), provider_choices: Mapping[str, str] | None = None,
               owner: str | None = None) -> dict[str, Any]:
        """Record deployment intent for a root. ``owner`` makes it private."""

        check_range(version_range)
        if not self.conn.execute(
            "SELECT 1 FROM composition_manifests WHERE name=?", [root]
        ).fetchone():
            raise CompositionError("not_installed", f"pack {root} has no installed manifest")
        record = [root, version_range, canonical(sorted(set(features))),
                  canonical(dict(sorted((provider_choices or {}).items()))), owner,
                  principal_id, self.now()]
        self.conn.execute("DELETE FROM composition_selections WHERE root_name=?", [root])
        self.conn.execute("INSERT INTO composition_selections VALUES (?,?,?,?,?,?,?)", record)
        self.audit("select", root, principal_id, {"range": version_range, "owner": owner})
        return self.selection()[root]

    def deselect(self, root: str, *, principal_id: str) -> bool:
        existed = self.conn.execute(
            "SELECT 1 FROM composition_selections WHERE root_name=?", [root]).fetchone()
        self.conn.execute("DELETE FROM composition_selections WHERE root_name=?", [root])
        if existed:
            self.audit("deselect", root, principal_id, {})
        return bool(existed)

    def selection(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT root_name, version_range, features_json, provider_choices_json, owner "
            "FROM composition_selections ORDER BY root_name").fetchall()
        return {
            row[0]: {"root": row[0], "range": row[1], "features": json.loads(row[2]),
                     "provider_choices": json.loads(row[3]), "owner": row[4]}
            for row in rows
        }

    @staticmethod
    def selection_hash(selection: Mapping[str, Any]) -> str:
        return digest(selection)

    def visible_roots(self, principal_id: str | None) -> set[str]:
        """Deployment-wide roots plus the caller's own private selections."""

        return {name for name, item in self.selection().items()
                if item["owner"] is None or item["owner"] == principal_id}

    # -- resolve ------------------------------------------------------------
    def store_plan(self, plan: Mapping[str, Any], selection: Mapping[str, Any]) -> str:
        value = validate_plan(plan)
        if not self.conn.execute(
            "SELECT 1 FROM composition_plans WHERE digest=?", [value["digest"]]
        ).fetchone():
            self.conn.execute(
                "INSERT INTO composition_plans VALUES (?,?,?,?,?)",
                [value["digest"], canonical(value), self.selection_hash(selection),
                 value["resolver_version"], self.now()],
            )
        return value["digest"]

    def plan(self, plan_digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT plan_json FROM composition_plans WHERE digest=?", [plan_digest]).fetchone()
        return None if row is None else json.loads(row[0])

    def plan_for_selection(self, selection: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT plan_json FROM composition_plans WHERE selection_hash=? "
            "ORDER BY created_at_ms DESC LIMIT 1", [self.selection_hash(selection)]).fetchone()
        return None if row is None else json.loads(row[0])

    # -- generations --------------------------------------------------------
    def active_generation(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT a.generation, g.plan_digest, g.activation_id, g.published_at_ms "
            "FROM composition_active a JOIN composition_generations g USING (generation) "
            "WHERE a.singleton=1").fetchone()
        if row is None:
            return None
        return {"generation": int(row[0]), "plan_digest": row[1], "activation_id": row[2],
                "published_at_ms": int(row[3])}

    def active_plan(self) -> dict[str, Any] | None:
        active = self.active_generation()
        return None if active is None else self.plan(active["plan_digest"])

    def generation_plans(self) -> dict[int, str]:
        return {int(row[0]): row[1] for row in self.conn.execute(
            "SELECT generation, plan_digest FROM composition_generations").fetchall()}

    # -- provider state, authority and run pins ----------------------------
    def provider_states(self) -> dict[str, str]:
        return {row[0]: row[1] for row in self.conn.execute(
            "SELECT provider_id, state FROM composition_provider_state").fetchall()}

    def set_provider_state(self, provider_id: str, state: str, *, reason: str, principal_id: str) -> None:
        self.conn.execute("DELETE FROM composition_provider_state WHERE provider_id=?", [provider_id])
        self.conn.execute("INSERT INTO composition_provider_state VALUES (?,?,?,?,?)",
                          [provider_id, state, reason, principal_id, self.now()])
        self.audit(f"provider-{state}", provider_id, principal_id, {"reason": reason})

    def authority(self) -> dict[str, dict[str, Any]]:
        return {row[0]: {"authority": row[1], "rolled_back": bool(row[2])} for row in self.conn.execute(
            "SELECT bundle, authority, rolled_back FROM composition_authority").fetchall()}

    def set_authority(self, bundle: str, authority: str, *, principal_id: str, rolled_back: bool = False) -> None:
        if authority not in {"legacy", "composition"}:
            raise CompositionError("invalid_authority", "authority is legacy or composition")
        self.conn.execute("DELETE FROM composition_authority WHERE bundle=?", [bundle])
        self.conn.execute("INSERT INTO composition_authority VALUES (?,?,?,?,?)",
                          [bundle, authority, principal_id, self.now(), bool(rolled_back)])
        self.audit(f"authority-{authority}", bundle, principal_id, {"rolled_back": rolled_back})

    def pin_run(self, run_id: str, plan_digest: str, *, owner: str) -> None:
        if self.plan(plan_digest) is None:
            raise CompositionError("unknown_plan", "runs can only pin stored plans")
        self.conn.execute(
            "INSERT INTO composition_run_pins VALUES (?,?,?,'active',?) ON CONFLICT (run_id) DO NOTHING",
            [run_id, plan_digest, owner, self.now()],
        )

    def release_run(self, run_id: str, status: str = "finished") -> None:
        self.conn.execute("UPDATE composition_run_pins SET status=? WHERE run_id=?", [status, run_id])

    def run_pin(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT plan_digest, owner, status FROM composition_run_pins WHERE run_id=?", [run_id]).fetchone()
        return None if row is None else {"run_id": run_id, "plan_digest": row[0], "owner": row[1], "status": row[2]}

    def pinned_plans(self) -> list[dict[str, Any]]:
        digests = [row[0] for row in self.conn.execute(
            "SELECT DISTINCT plan_digest FROM composition_run_pins WHERE status='active'").fetchall()]
        return [plan for plan in (self.plan(d) for d in sorted(digests)) if plan is not None]


__all__ = ["DDL", "TABLES", "CompositionStore", "ensure_schema", "initialized"]
