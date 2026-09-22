"""Versioned structured assertions and cross-visibility comparisons.

This small store is connector-agnostic. It records what a source asserted at
an effective time; it does not infer assertions from prose or grant access.
Callers must authorize private reads before invoking ``compare_assertions``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS versioned_assertions (
    subject_id TEXT NOT NULL,
    assertion_key TEXT NOT NULL,
    assertion_value_json TEXT NOT NULL,
    effective_at_ms BIGINT NOT NULL,
    document_id TEXT NOT NULL,
    visibility TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    PRIMARY KEY (subject_id, assertion_key, effective_at_ms, document_id)
);
"""


def ensure_assertion_schema(conn: Any) -> None:
    conn.execute(_DDL)


def record_assertions(
    conn: Any,
    subject_id: str,
    assertions: Mapping[str, Any],
    *,
    effective_at_ms: int,
    document_id: str,
    visibility: str = "public",
    record_kind: str = "source_record",
) -> int:
    """Idempotently record a set of source-provided structured assertions."""
    if visibility not in {"public", "private"}:
        raise ValueError("visibility must be public or private")
    ensure_assertion_schema(conn)
    for key, value in sorted(assertions.items()):
        conn.execute(
            "INSERT OR REPLACE INTO versioned_assertions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                str(subject_id),
                str(key),
                json.dumps(value, sort_keys=True),
                int(effective_at_ms),
                str(document_id),
                visibility,
                str(record_kind),
            ],
        )
    return len(assertions)


def compare_assertions(conn: Any, subject_id: str) -> dict[str, Any]:
    """Compare the newest private assertions with newer public assertions.

    Authorization is deliberately outside this storage primitive. The return
    value contains private document IDs and must only cross an authorized
    boundary.
    """
    ensure_assertion_schema(conn)
    rows = conn.execute(
        "SELECT assertion_key, assertion_value_json, effective_at_ms, document_id, "
        "visibility, record_kind FROM versioned_assertions WHERE subject_id = ? "
        "ORDER BY assertion_key, effective_at_ms, document_id",
        [str(subject_id)],
    ).fetchall()
    by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for key, value, effective, document_id, visibility, kind in rows:
        by_key.setdefault(key, {"public": [], "private": []})[visibility].append(
            {
                "value": json.loads(value),
                "effective_at_ms": int(effective),
                "document_id": document_id,
                "record_kind": kind,
            }
        )
    differences = []
    for key, values in sorted(by_key.items()):
        if not values["public"] or not values["private"]:
            continue
        public = values["public"][-1]
        private = values["private"][-1]
        if (
            public["effective_at_ms"] > private["effective_at_ms"]
            and public["value"] != private["value"]
        ):
            differences.append(
                {"assertion_key": key, "public": public, "private": private}
            )
    comparison_id = hashlib.sha256(
        json.dumps(differences, sort_keys=True).encode()
    ).hexdigest()[:24]
    private_document_ids = sorted(
        {
            row["document_id"]
            for values in by_key.values()
            for row in values["private"]
        }
    )
    return {
        "stale": bool(differences),
        "comparison_id": comparison_id,
        "differences": differences,
        "private_document_ids": private_document_ids,
        "n": len(differences),
        "method": "latest-effective-versioned-assertion-comparison",
        "assumptions": [
            "private visibility was authorized by the caller",
            "effective timestamps describe source applicability",
        ],
    }


__all__ = ["compare_assertions", "ensure_assertion_schema", "record_assertions"]
