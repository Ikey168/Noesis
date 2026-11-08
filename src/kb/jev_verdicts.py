"""Apply a linking run's stored Jev verdicts after the fact (no new Jev calls).

A shadow-mode run writes every planned link and stores Jev's verdict for each
in ``claim_link_jev_assessments``. :func:`apply_jev_verdicts` then does what
gate mode would have done, from those stored verdicts alone:

* a link Jev answered against is **withheld** (moved to the reversible
  withheld store, never deleted), together with any temporal transition the
  same run derived from it;
* a ``supports`` link Jev read in the opposite direction (``b_supports_a``)
  is **reversed**: the original is withheld and the support is written from
  the other claim to this one.

Nothing is changed unless ``apply=True``; the default returns the plan.
Jev's mistakes stay visible in the stored verdicts. ``keep`` (or
:func:`keep_links` later) records an operator override in
``claim_link_jev_overrides``: kept links are left as they are, a withheld or
reversed one is restored, and future gate-mode runs write them as planned.
:func:`undo_jev_verdicts` reverts an applied operation exactly.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

from src.kb.link_withholding import (
    ensure_withholding_schema,
    insert_replacement,
    record_op,
    restore_op,
    withheld_row,
    withhold,
)

TEMPORAL = ("supersedes", "corrects", "retracts")

_REVIEW_STATE = """
CREATE TABLE IF NOT EXISTS claim_link_review_state (
    claim_a    TEXT NOT NULL,
    claim_b    TEXT NOT NULL,
    relation   TEXT NOT NULL,
    state      TEXT NOT NULL,
    jev_run_id TEXT NOT NULL,
    op_id      TEXT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (claim_a, claim_b, relation)
)
"""
#: Review state per stored link after its verdicts are applied.
REVIEW_STATES = ("confirmed", "reversed", "withheld", "unreviewed", "kept_override")

_OVERRIDES = """
CREATE TABLE IF NOT EXISTS claim_link_jev_overrides (
    claim_a    TEXT NOT NULL,
    claim_b    TEXT NOT NULL,
    relation   TEXT NOT NULL,
    note       TEXT,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (claim_a, claim_b, relation)
)
"""

Key = tuple[str, str, str]


def ensure_override_schema(conn) -> None:
    conn.execute(_OVERRIDES)
    conn.execute(_REVIEW_STATE)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone() is not None


def overridden_keys(conn, keys: Iterable[Key]) -> set[Key]:
    """Which of ``keys`` an operator has told gate mode to keep."""
    if not _table_exists(conn, "claim_link_jev_overrides"):
        return set()
    stored = {tuple(row) for row in conn.execute(
        "SELECT claim_a, claim_b, relation FROM claim_link_jev_overrides"
    ).fetchall()}
    return {key for key in keys if key in stored}


def _link(conn, key: Key) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT domain_a, domain_b, run_id FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
        list(key),
    ).fetchone()
    return {"domain_a": row[0], "domain_b": row[1], "run_id": row[2]} if row else None


def plan_jev_verdicts(conn, run_id: str, *, keep: Iterable[Key] = ()) -> dict[str, Any]:
    """What applying ``run_id``'s stored verdicts would change."""
    if not _table_exists(conn, "claim_link_jev_assessments"):
        raise ValueError("no stored Jev assessments in this warehouse")
    keep = set(keep)
    if _table_exists(conn, "claim_link_jev_overrides"):
        keep |= {tuple(r) for r in conn.execute(
            "SELECT claim_a, claim_b, relation FROM claim_link_jev_overrides").fetchall()}
    rows = conn.execute(
        """
        SELECT claim_a, claim_b, relation, status, verdict, action, jev_relation, detail_json
        FROM claim_link_jev_assessments WHERE run_id = ?
        ORDER BY relation, claim_a, claim_b
        """,
        [run_id],
    ).fetchall()
    if not rows:
        raise ValueError(f"run {run_id!r} has no stored Jev assessments")
    withhold_keys: list[Key] = []
    reverse_keys: list[Key] = []
    states: dict[Key, str] = {}
    dependents: dict[Key, list[Key]] = {}
    counts: Counter = Counter()
    kept: list[Key] = []
    for claim_a, claim_b, relation, status, verdict, action, jev_relation, _ in rows:
        key = (claim_a, claim_b, relation)
        counts[f"{relation}:{status}:{verdict}"] += 1
        if verdict != "disagrees":
            states[key] = "confirmed" if verdict == "agrees" else "unreviewed"
            continue
        if action in ("withheld", "reversed"):
            counts["already_applied_by_gate"] += 1
            states[key] = action
            continue
        if key in keep:
            kept.append(key)
            states[key] = "kept_override"
            continue
        if _link(conn, key) is None:
            counts["link_missing"] += 1
            continue
        if relation == "supports" and jev_relation == "b_supports_a":
            reverse_keys.append(key)
            states[key] = "reversed"
            continue
        withhold_keys.append(key)
        states[key] = "withheld"
        if relation == "duplicate":
            deps = [tuple(r) for r in conn.execute(
                "SELECT claim_a, claim_b, relation FROM claim_links WHERE run_id = ?"
                " AND relation IN ('supersedes', 'corrects', 'retracts')"
                " AND ((claim_a = ? AND claim_b = ?) OR (claim_a = ? AND claim_b = ?))",
                [run_id, claim_a, claim_b, claim_b, claim_a],
            ).fetchall()]
            if deps:
                dependents[key] = deps
    by_relation: Counter = Counter()
    for key in withhold_keys:
        by_relation[f"withhold:{key[2]}"] += 1
    for key in reverse_keys:
        by_relation[f"reverse:{key[2]}"] += 1
    return {
        "run_id": run_id,
        "assessments": len(rows),
        "withhold": withhold_keys,
        "reverse": reverse_keys,
        "dependents": dependents,
        "kept": kept,
        "states": states,
        "summary": {
            "states": dict(sorted(Counter(states.values()).items())),
            "withheld": len(withhold_keys),
            "dependent_transitions_withheld": sum(len(v) for v in dependents.values()),
            "reversed": len(reverse_keys),
            "kept_by_override": len(kept),
            "by_action": dict(sorted(by_relation.items())),
            "assessment_counts": dict(sorted(counts.items())),
        },
    }


def apply_jev_verdicts(
    conn, run_id: str, *, apply: bool = False, keep: Iterable[Key] = (),
    note: str | None = None, op_id: str | None = None,
) -> dict[str, Any]:
    """Withhold rejected links and reverse reversed supports for ``run_id``.

    Returns the plan's summary; with ``apply=True`` also the ``op_id`` to
    pass to :func:`undo_jev_verdicts`. ``keep`` entries are recorded as
    overrides (with ``note``) when applied.
    """
    ensure_withholding_schema(conn)
    ensure_override_schema(conn)
    keep = [tuple(k) for k in keep]
    plan = plan_jev_verdicts(conn, run_id, keep=keep)
    result = {"run_id": run_id, "applied": False, **plan["summary"]}
    if not apply:
        result["sample"] = {
            "withhold": plan["withhold"][:20],
            "reverse": plan["reverse"][:20],
        }
        return result
    op_id = op_id or f"jev-verdicts:{run_id}:{int(time.time() * 1000)}"
    now = int(time.time() * 1000)
    conn.execute("BEGIN TRANSACTION")
    try:
        for key in keep:
            conn.execute(
                "INSERT INTO claim_link_jev_overrides VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (claim_a, claim_b, relation) DO UPDATE SET note = excluded.note,"
                " created_at = excluded.created_at",
                [*key, note, now],
            )
        detail = {"jev_run_id": run_id}
        withhold(conn, op_id, plan["withhold"], "jev_rejected",
                 {key: {**detail, "verdict": "disagrees"} for key in plan["withhold"]})
        for parent, deps in plan["dependents"].items():
            withhold(conn, op_id, deps, "jev_rejected_dependent",
                     {dep: {**detail, "parent": list(parent)} for dep in deps})
        reversed_written = 0
        for key in plan["reverse"]:
            withhold(conn, op_id, [key], "jev_reversed", {key: {**detail, "jev_relation": "b_supports_a"}})
            original = withheld_row(conn, op_id, key)
            reversed_written += insert_replacement(
                conn, op_id, original, claim_a=original["claim_b"], claim_b=original["claim_a"],
                domain_a=original["domain_b"], domain_b=original["domain_a"],
                method_suffix="+jev_reversed",
            )
        for key, state in plan["states"].items():
            conn.execute(
                "INSERT INTO claim_link_review_state VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (claim_a, claim_b, relation) DO UPDATE SET state = excluded.state,"
                " jev_run_id = excluded.jev_run_id, op_id = excluded.op_id,"
                " updated_at = excluded.updated_at",
                [*key, state, run_id, op_id, now],
            )
        result.update({"applied": True, "op_id": op_id, "reversed_links_written": reversed_written})
        record_op(conn, op_id, "jev_verdicts", run_id, result)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    result["clusters"] = _rebuild_clusters(conn, op_id)
    return result


def _rebuild_clusters(conn, op_id: str) -> dict[str, Any]:
    """Clusters derive from duplicate links, so recompute them after a change."""
    from src.kb.clusters import run_clustering_pass

    return run_clustering_pass(conn, run_id=f"{op_id}:clusters")


def undo_jev_verdicts(conn, op_id: str) -> dict[str, int]:
    """Revert an applied verdict operation: withheld links return, reversals go."""
    ensure_withholding_schema(conn)
    ensure_override_schema(conn)
    conn.execute("BEGIN TRANSACTION")
    try:
        result = restore_op(conn, op_id)
        conn.execute("DELETE FROM claim_link_review_state WHERE op_id = ?", [op_id])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    result["clusters"] = _rebuild_clusters(conn, op_id)
    return result


def keep_links(conn, keys: Iterable[Key], *, note: str | None = None) -> dict[str, int]:
    """Override Jev for specific links (e.g. a "sentence plus hashes" mistake).

    Records the override so gate-mode runs write the link as planned, and
    restores the link if a verdict operation already withheld or reversed it.
    """
    ensure_withholding_schema(conn)
    ensure_override_schema(conn)
    keys = [tuple(k) for k in keys]
    now = int(time.time() * 1000)
    restored = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for key in keys:
            conn.execute(
                "INSERT INTO claim_link_jev_overrides VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (claim_a, claim_b, relation) DO UPDATE SET note = excluded.note,"
                " created_at = excluded.created_at",
                [*key, note, now],
            )
            ops = [row[0] for row in conn.execute(
                "SELECT DISTINCT w.op_id FROM claim_links_withheld w"
                " JOIN claim_link_ops o ON o.op_id = w.op_id"
                " WHERE o.kind = 'jev_verdicts' AND w.claim_a = ? AND w.claim_b = ? AND w.relation = ?",
                list(key),
            ).fetchall()]
            for op_id in ops:
                dependents = [tuple(r) for r in conn.execute(
                    "SELECT claim_a, claim_b, relation FROM claim_links_withheld"
                    " WHERE op_id = ? AND reason = 'jev_rejected_dependent' AND detail_json LIKE ?",
                    [op_id, f"%{json.dumps(list(key))[1:-1]}%"],
                ).fetchall()]
                restored += restore_op(conn, op_id, keys=[key, *dependents])["restored"]
            conn.execute(
                "UPDATE claim_link_review_state SET state = 'kept_override', updated_at = ?"
                " WHERE claim_a = ? AND claim_b = ? AND relation = ?",
                [now, *key],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    clusters = _rebuild_clusters(conn, f"keep-{now}") if restored else None
    return {"overrides": len(keys), "links_restored": restored, "clusters": clusters}
