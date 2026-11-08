"""Reversible withholding of stored claim links.

Links are never deleted by the clean-up operations that use this module.
A withheld link is moved, with every column intact, to
``claim_links_withheld`` under an ``op_id``; a replacement link (for example
a support whose direction was reversed) is recorded in
``claim_link_replacements``. :func:`restore_op` undoes an operation exactly:
the withheld rows return and its replacements are removed, unless a later
run has since rewritten them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

_COLUMNS = (
    "domain_a", "claim_a", "domain_b", "claim_b", "relation", "score", "method",
    "prediction_mode", "confidence", "model_version", "run_id", "created_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_links_withheld (
    op_id           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    detail_json     TEXT NOT NULL,
    withheld_at     BIGINT NOT NULL,
    domain_a        TEXT NOT NULL,
    claim_a         TEXT NOT NULL,
    domain_b        TEXT NOT NULL,
    claim_b         TEXT NOT NULL,
    relation        TEXT NOT NULL,
    score           DOUBLE NOT NULL,
    method          TEXT NOT NULL,
    prediction_mode TEXT NOT NULL,
    confidence      DOUBLE,
    model_version   TEXT,
    run_id          TEXT NOT NULL,
    created_at      BIGINT NOT NULL,
    PRIMARY KEY (op_id, claim_a, claim_b, relation)
);
CREATE TABLE IF NOT EXISTS claim_link_replacements (
    op_id           TEXT NOT NULL,
    claim_a         TEXT NOT NULL,
    claim_b         TEXT NOT NULL,
    relation        TEXT NOT NULL,
    method          TEXT NOT NULL,
    source_claim_a  TEXT NOT NULL,
    source_claim_b  TEXT NOT NULL,
    source_relation TEXT NOT NULL,
    PRIMARY KEY (op_id, claim_a, claim_b, relation)
);
CREATE TABLE IF NOT EXISTS claim_link_ops (
    op_id        TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    subject      TEXT,
    summary_json TEXT NOT NULL,
    created_at   BIGINT NOT NULL,
    undone_at    BIGINT
);
"""


def ensure_withholding_schema(conn) -> None:
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _now() -> int:
    return int(time.time() * 1000)


def withhold(
    conn, op_id: str, keys: Iterable[tuple[str, str, str]], reason: str,
    details: Mapping[tuple[str, str, str], Mapping[str, Any]] | None = None,
) -> int:
    """Move the ``(claim_a, claim_b, relation)`` links into the withheld store.

    Keys that are not (or no longer) in ``claim_links`` are skipped. Returns
    the number of links moved. Call inside the caller's transaction.
    """
    moved = 0
    columns = ", ".join(_COLUMNS)
    for key in dict.fromkeys(keys):
        row = conn.execute(
            f"SELECT {columns} FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
            list(key),
        ).fetchone()
        if row is None:
            continue
        detail = dict((details or {}).get(key) or {})
        conn.execute(
            f"INSERT INTO claim_links_withheld (op_id, reason, detail_json, withheld_at, {columns})"
            f" VALUES (?, ?, ?, ?, {', '.join('?' for _ in _COLUMNS)})"
            " ON CONFLICT (op_id, claim_a, claim_b, relation) DO NOTHING",
            [op_id, reason, json.dumps(detail, sort_keys=True), _now(), *row],
        )
        conn.execute(
            "DELETE FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
            list(key),
        )
        moved += 1
    return moved


def insert_replacement(conn, op_id: str, original: Mapping[str, Any], *, claim_a: str,
                       claim_b: str, domain_a: str, domain_b: str, method_suffix: str) -> bool:
    """Write a replacement link derived from a withheld one.

    An existing link with the same key is kept (it is not this operation's
    to overwrite). Returns True when the replacement was written.
    """
    relation = original["relation"]
    exists = conn.execute(
        "SELECT 1 FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
        [claim_a, claim_b, relation],
    ).fetchone()
    if exists:
        return False
    method = f"{original['method']}{method_suffix}"
    conn.execute(
        f"INSERT INTO claim_links ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
        [domain_a, claim_a, domain_b, claim_b, relation, original["score"], method,
         original["prediction_mode"], original["confidence"], original["model_version"],
         original["run_id"], _now()],
    )
    conn.execute(
        "INSERT INTO claim_link_replacements VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [op_id, claim_a, claim_b, relation, method,
         original["claim_a"], original["claim_b"], relation],
    )
    return True


def withheld_row(conn, op_id: str, key: tuple[str, str, str]) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM claim_links_withheld"
        " WHERE op_id = ? AND claim_a = ? AND claim_b = ? AND relation = ?",
        [op_id, *key],
    ).fetchone()
    return dict(zip(_COLUMNS, row, strict=True)) if row else None


def record_op(conn, op_id: str, kind: str, subject: str | None, summary: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO claim_link_ops VALUES (?, ?, ?, ?, ?, NULL)"
        " ON CONFLICT (op_id) DO UPDATE SET summary_json = excluded.summary_json,"
        " created_at = excluded.created_at, undone_at = NULL",
        [op_id, kind, subject, json.dumps(dict(summary), sort_keys=True, default=str), _now()],
    )


def restore_op(conn, op_id: str, *, keys: Iterable[tuple[str, str, str]] | None = None) -> dict[str, int]:
    """Undo an operation (or only ``keys`` of it). Call inside a transaction.

    Withheld links return to ``claim_links`` unless a link with the same key
    has been written there since (the newer one is kept). Replacements are
    removed only while they still carry this operation's method.
    """
    ensure_withholding_schema(conn)
    columns = ", ".join(_COLUMNS)
    where, src_where, params = "op_id = ?", "op_id = ?", [op_id]
    if keys is not None:
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {"restored": 0, "replacements_removed": 0, "kept_newer": 0}
        match = " OR ".join("(claim_a = ? AND claim_b = ? AND relation = ?)" for _ in keys)
        src_match = " OR ".join(
            "(source_claim_a = ? AND source_claim_b = ? AND source_relation = ?)" for _ in keys
        )
        where += f" AND ({match})"
        src_where += f" AND ({src_match})"
        params += [value for key in keys for value in key]
    rows = conn.execute(f"SELECT {columns} FROM claim_links_withheld WHERE {where}", params).fetchall()
    restored = kept_newer = removed = 0
    replacements = conn.execute(
        f"SELECT claim_a, claim_b, relation, method FROM claim_link_replacements WHERE {src_where}",
        params,
    ).fetchall()
    for claim_a, claim_b, relation, method in replacements:
        current = conn.execute(
            "SELECT method FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
            [claim_a, claim_b, relation],
        ).fetchone()
        if current and current[0] == method:
            conn.execute(
                "DELETE FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?",
                [claim_a, claim_b, relation],
            )
            removed += 1
        conn.execute(
            "DELETE FROM claim_link_replacements WHERE op_id = ? AND claim_a = ? AND claim_b = ? AND relation = ?",
            [op_id, claim_a, claim_b, relation],
        )
    for row in rows:
        key = (row[1], row[3], row[4])
        if conn.execute(
            "SELECT 1 FROM claim_links WHERE claim_a = ? AND claim_b = ? AND relation = ?", list(key)
        ).fetchone():
            kept_newer += 1
        else:
            conn.execute(
                f"INSERT INTO claim_links ({columns}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                list(row),
            )
            restored += 1
        conn.execute(
            "DELETE FROM claim_links_withheld WHERE op_id = ? AND claim_a = ? AND claim_b = ? AND relation = ?",
            [op_id, *key],
        )
    if keys is None:
        conn.execute("UPDATE claim_link_ops SET undone_at = ? WHERE op_id = ?", [_now(), op_id])
    return {"restored": restored, "replacements_removed": removed, "kept_newer": kept_newer}
