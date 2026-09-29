"""Composition dependents of source packs (C06.1).

Source pins and cursors stay owned by the source-pack store; composition
plans reference them by ``(pack_id, version[, range, manifest_hash])`` and
never copy them. This module only *reads* the composition tables so the
source-pack upgrade preview can list plans that depend on a source pack.
"""

from __future__ import annotations

import json
from typing import Any

READ_SCOPE = "knowledge:composition:read"


def _tables(conn: Any) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}


def composition_dependents(conn: Any, pack_id: str) -> list[dict[str, Any]]:
    """Plans that pin ``pack_id``: the active generation and generations pinned by runs."""

    tables = _tables(conn)
    if not {"composition_generations", "composition_plans", "composition_active"} <= tables:
        return []
    generations: dict[str, str] = {}
    for (generation_id,) in conn.execute("SELECT generation_id FROM composition_active WHERE slot=1").fetchall():
        generations[generation_id] = "active"
    if "composition_run_pins" in tables:
        for (generation_id,) in conn.execute("SELECT DISTINCT generation_id FROM composition_run_pins").fetchall():
            generations.setdefault(generation_id, "pinned-run")
    dependents = []
    for generation_id, role in sorted(generations.items()):
        row = conn.execute("SELECT p.digest, p.plan_json FROM composition_generations g JOIN composition_plans p "
                           "ON p.digest=g.plan_digest WHERE g.generation_id=?", [generation_id]).fetchone()
        if not row:
            continue
        plan = json.loads(row[1])
        for pin in plan.get("source_packs") or []:
            if pin.get("pack_id") == pack_id:
                dependents.append({"plan_digest": row[0], "generation": generation_id, "role": role, "pin": pin})
    return dependents


def visible(scopes: Any) -> bool:
    return "operator" in scopes or READ_SCOPE in scopes
