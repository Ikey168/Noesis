"""Reversible exclusion of boilerplate claims.

Claims are never deleted here. :func:`run_claim_exclusion_pass` records each
excluded claim in ``argument_claim_exclusions`` with a reason and the pass's
``run_id``:

* ``repeated_on_site`` — the same wording (see
  :func:`src.argument_mining.boilerplate.normalize`) occurs in at least
  ``min_site_documents`` distinct documents of one site (``source_id``, or
  the URL host when no source is recorded): page furniture, not a claim;
* ``too_short``, ``navigation``, ``share``, ``byline``, ``marketing`` — see
  :func:`src.argument_mining.boilerplate.boilerplate_reason`.

The claim linking pass, clustering and the conflict graph skip excluded
claims. With ``withhold_links`` (default), existing links touching a newly
excluded claim are moved to the reversible withheld store under the same
``run_id``. :func:`undo_claim_exclusions` reverts a pass exactly;
:func:`restore_claims` reinstates individual claims and keeps later passes
from excluding them again.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from src.argument_mining.boilerplate import boilerplate_reason, normalize

REASONS = ("repeated_on_site", "too_short", "navigation", "share", "byline", "marketing")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS argument_claim_exclusions (
        claim_id    TEXT PRIMARY KEY,
        reason      TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        run_id      TEXT NOT NULL,
        excluded_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS argument_claim_exclusion_overrides (
        claim_id   TEXT PRIMARY KEY,
        note       TEXT,
        created_at BIGINT NOT NULL
    )
    """,
)

#: SQL predicate for "claim alias ``c`` is not excluded" (schema must exist).
NOT_EXCLUDED = (
    "NOT EXISTS (SELECT 1 FROM argument_claim_exclusions x WHERE x.claim_id = {alias}.claim_id)"
)


def ensure_exclusion_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)
    if conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'argument_claims'"
    ).fetchone():
        conn.execute("ALTER TABLE argument_claims ADD COLUMN IF NOT EXISTS excluded_reason VARCHAR")


def sync_excluded_reason(conn) -> None:
    """Mirror ``argument_claim_exclusions`` into ``argument_claims.excluded_reason``.

    The side table stays authoritative (it carries run ids for undo); the
    column lets any reader filter with ``excluded_reason IS NULL``.
    Re-extracted claims (deleted and reinserted) pick their flag back up.
    """
    conn.execute(
        "UPDATE argument_claims SET excluded_reason = x.reason FROM argument_claim_exclusions x"
        " WHERE x.claim_id = argument_claims.claim_id"
        " AND (argument_claims.excluded_reason IS NULL OR argument_claims.excluded_reason <> x.reason)"
    )
    conn.execute(
        "UPDATE argument_claims SET excluded_reason = NULL WHERE excluded_reason IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM argument_claim_exclusions x"
        " WHERE x.claim_id = argument_claims.claim_id)"
    )


def not_excluded(alias: str = "c") -> str:
    return NOT_EXCLUDED.format(alias=alias)


def _site(source_id: str | None, url: str | None) -> str | None:
    if source_id:
        return f"source:{source_id}"
    if url:
        host = (urlsplit(url).hostname or "").lower()
        host = host.removeprefix("www.")
        return f"host:{host}" if host else None
    return None


def plan_exclusions(conn, *, min_site_documents: int = 3) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{claim_id: (reason, detail)}`` for claims that should be excluded."""
    if type(min_site_documents) is not int or min_site_documents < 2:
        raise ValueError("min_site_documents must be an integer of at least 2")
    rows = conn.execute(
        """
        SELECT c.claim_id, c.claim_text, c.document_id, d.source_id, d.url
        FROM argument_claims c
        LEFT JOIN documents d ON d.document_id = c.document_id
        """
    ).fetchall()
    planned: dict[str, tuple[str, dict[str, Any]]] = {}
    by_wording: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for claim_id, text, document_id, source_id, url in rows:
        reason = boilerplate_reason(text or "")
        if reason:
            planned[claim_id] = (reason, {})
            continue
        site = _site(source_id, url)
        wording = normalize(text or "")
        if site and wording:
            by_wording[(site, wording)].append((claim_id, document_id))
    for (site, _), members in by_wording.items():
        documents = {document_id for _, document_id in members}
        if len(documents) >= min_site_documents:
            for claim_id, _ in members:
                planned[claim_id] = ("repeated_on_site", {"site": site, "documents": len(documents)})
    return planned


def run_claim_exclusion_pass(
    conn, *, run_id: str | None = None, min_site_documents: int = 3,
    withhold_links: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    """Mark boilerplate claims excluded (never deleted); returns a summary."""
    from src.kb.claim_links import ensure_claim_link_schema
    from src.kb.link_withholding import ensure_withholding_schema, record_op, withhold

    ensure_claim_link_schema(conn)
    ensure_exclusion_schema(conn)
    ensure_withholding_schema(conn)
    run_id = run_id or f"kb-claim-exclusions-{uuid.uuid4().hex[:12]}"
    already = {row[0] for row in conn.execute("SELECT claim_id FROM argument_claim_exclusions").fetchall()}
    kept = {row[0] for row in conn.execute("SELECT claim_id FROM argument_claim_exclusion_overrides").fetchall()}
    planned = {
        claim_id: value
        for claim_id, value in plan_exclusions(conn, min_site_documents=min_site_documents).items()
        if claim_id not in already and claim_id not in kept
    }
    by_reason = {reason: 0 for reason in REASONS}
    for reason, _ in planned.values():
        by_reason[reason] += 1
    claims_total = conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()[0]
    link_keys: list[tuple[str, str, str]] = []
    if withhold_links and planned:
        ids = sorted(planned)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            link_keys += [
                tuple(row) for row in conn.execute(
                    f"SELECT claim_a, claim_b, relation FROM claim_links"
                    f" WHERE claim_a IN ({marks}) OR claim_b IN ({marks})",
                    [*chunk, *chunk],
                ).fetchall()
            ]
    link_keys = list(dict.fromkeys(link_keys))
    links_by_relation: dict[str, int] = defaultdict(int)
    for _, _, relation in link_keys:
        links_by_relation[relation] += 1
    summary = {
        "run_id": run_id,
        "dry_run": dry_run,
        "claims_total": int(claims_total),
        "claims_excluded": len(planned),
        "already_excluded": len(already),
        "kept_by_override": len(kept),
        "by_reason": by_reason,
        "links_withheld": len(link_keys),
        "links_withheld_by_relation": dict(sorted(links_by_relation.items())),
    }
    if dry_run:
        return summary
    if not planned:
        sync_excluded_reason(conn)
        return summary
    now = int(time.time() * 1000)
    conn.execute("BEGIN TRANSACTION")
    try:
        for claim_id, (reason, detail) in sorted(planned.items()):
            conn.execute(
                "INSERT INTO argument_claim_exclusions VALUES (?, ?, ?, ?, ?)",
                [claim_id, reason, json.dumps(detail, sort_keys=True), run_id, now],
            )
        if link_keys:
            details = {}
            for key in link_keys:
                excluded = [claim for claim in key[:2] if claim in planned]
                details[key] = {"excluded_claims": excluded,
                                "reasons": sorted({planned[c][0] for c in excluded})}
            withhold(conn, run_id, link_keys, "claim_excluded", details)
        record_op(conn, run_id, "claim_exclusions", None, summary)
        sync_excluded_reason(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def undo_claim_exclusions(conn, run_id: str) -> dict[str, int]:
    """Revert one exclusion pass: its claims return and its links are restored."""
    from src.kb.link_withholding import ensure_withholding_schema, restore_op

    ensure_exclusion_schema(conn)
    ensure_withholding_schema(conn)
    conn.execute("BEGIN TRANSACTION")
    try:
        claims = conn.execute(
            "SELECT COUNT(*) FROM argument_claim_exclusions WHERE run_id = ?", [run_id]
        ).fetchone()[0]
        conn.execute("DELETE FROM argument_claim_exclusions WHERE run_id = ?", [run_id])
        links = restore_op(conn, run_id)
        sync_excluded_reason(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"claims_restored": int(claims), "links_restored": links["restored"],
            "links_kept_newer": links["kept_newer"]}


def restore_claims(conn, claim_ids: Iterable[str], *, note: str | None = None) -> dict[str, int]:
    """Reinstate individual claims and stop later passes from excluding them.

    Their withheld links return when the other endpoint is not excluded.
    """
    from src.kb.link_withholding import ensure_withholding_schema, restore_op

    ensure_exclusion_schema(conn)
    ensure_withholding_schema(conn)
    claim_ids = sorted(set(claim_ids))
    if not claim_ids:
        return {"claims_restored": 0, "links_restored": 0}
    now = int(time.time() * 1000)
    restored_links = restored_claims = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for claim_id in claim_ids:
            restored_claims += conn.execute(
                "SELECT COUNT(*) FROM argument_claim_exclusions WHERE claim_id = ?", [claim_id]
            ).fetchone()[0]
            conn.execute("DELETE FROM argument_claim_exclusions WHERE claim_id = ?", [claim_id])
            conn.execute(
                "INSERT INTO argument_claim_exclusion_overrides VALUES (?, ?, ?)"
                " ON CONFLICT (claim_id) DO UPDATE SET note = excluded.note, created_at = excluded.created_at",
                [claim_id, note, now],
            )
        excluded = {row[0] for row in conn.execute("SELECT claim_id FROM argument_claim_exclusions").fetchall()}
        marks = ", ".join("?" for _ in claim_ids)
        rows = conn.execute(
            f"SELECT op_id, claim_a, claim_b, relation FROM claim_links_withheld"
            f" WHERE reason = 'claim_excluded' AND (claim_a IN ({marks}) OR claim_b IN ({marks}))",
            [*claim_ids, *claim_ids],
        ).fetchall()
        by_op: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for op_id, claim_a, claim_b, relation in rows:
            if claim_a not in excluded and claim_b not in excluded:
                by_op[op_id].append((claim_a, claim_b, relation))
        for op_id, keys in by_op.items():
            restored_links += restore_op(conn, op_id, keys=keys)["restored"]
        sync_excluded_reason(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"claims_restored": int(restored_claims), "links_restored": restored_links}
