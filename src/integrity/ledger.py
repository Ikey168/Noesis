"""Unified per-document integrity ledger.

Snapshots, re-fetch revisions, correction notices, image appearances/C2PA,
reuse, and cross-modal checks previously existed as disconnected analytics.
This module composes them into one evidence-locator-first, honesty-enveloped
read view. It does not manufacture a single trust score: heterogeneous signals
remain separately interpretable and absence of C2PA is explicitly neutral.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from src.analytics.honesty import analytic_envelope
from src.osint.evidence import citation, document_citations

METHOD = "integrity ledger aggregation v1"
ASSUMPTIONS = [
    "the ledger reports observed provenance signals, not a truth or authenticity score",
    "missing C2PA credentials are neutral, not evidence of manipulation",
    "cross-modal and perceptual-hash findings require human review",
]


def _table(conn, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone())
    except Exception:
        return False


def _document(conn, document_id: str) -> Optional[dict[str, Any]]:
    if not _table(conn, "documents"):
        return None
    row = conn.execute(
        """SELECT document_id, source_type, source_id, url, title, content_hash,
                  ingested_at, created_at FROM documents WHERE document_id = ?""",
        [document_id],
    ).fetchone()
    if row is None:
        return None
    keys = ("document_id", "source_type", "source_id", "url", "title",
            "content_hash", "ingested_at", "created_at")
    return dict(zip(keys, row))


def _version_locator(doc: dict[str, Any], revision: int, content_hash: str,
                     fetched_at: Optional[int]) -> dict[str, Any]:
    locator = citation(doc["document_id"], doc.get("source_id"), doc.get("url"),
                       chunk=f"revision-{revision}", resolved=True)
    locator.update(revision=revision, content_hash=content_hash,
                   fetched_at=fetched_at, archived=True)
    return locator


def _revisions(conn, doc: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    if not _table(conn, "document_revisions"):
        return [], []
    rows = conn.execute(
        """SELECT revision, content_hash, change_class, fetched_at
           FROM document_revisions WHERE document_id = ? ORDER BY revision""",
        [doc["document_id"]],
    ).fetchall()
    versions = [
        {"revision": r[0], "content_hash": r[1], "change_class": r[2],
         "fetched_at": r[3],
         "evidence": _version_locator(doc, r[0], r[1], r[3])}
        for r in rows
    ]
    findings = []
    for previous, current in zip(versions, versions[1:]):
        if current["change_class"] == "unchanged":
            continue
        findings.append({
            "kind": "document_revision",
            "severity": "high" if current["change_class"] in {
                "silent_substantive", "retraction", "takedown"
            } else "info",
            "change_class": current["change_class"],
            # Both sides are mandatory, especially for silent edits.
            "evidence": [previous["evidence"], current["evidence"]],
        })
    return versions, findings


def _snapshots(conn, doc: dict[str, Any]) -> list[dict[str, Any]]:
    if not doc.get("url") or not _table(conn, "url_snapshots"):
        return []
    return [
        {"url": r[0], "fetched_at": r[1], "content_hash": r[2], "status": r[3],
         "evidence": {**citation(doc["document_id"], doc.get("source_id"), r[0],
                                 chunk=f"snapshot-{r[1]}", resolved=True),
                      "content_hash": r[2], "fetched_at": r[1], "archived": True}}
        for r in conn.execute(
            """SELECT url, fetched_at, content_hash, status FROM url_snapshots
               WHERE url = ? ORDER BY fetched_at""", [doc["url"]]
        ).fetchall()
    ]


def _assets(conn, document_id: str) -> tuple[list[dict], list[dict]]:
    if not (_table(conn, "image_assets") and _table(conn, "image_appearances")):
        return [], []
    rows = conn.execute(
        """SELECT a.sha256, a.path, a.mime, a.phash, a.exif, a.c2pa,
                  p.first_seen_at, p.context
           FROM image_appearances p JOIN image_assets a ON a.sha256=p.sha256
           WHERE p.document_id = ? ORDER BY p.first_seen_at NULLS LAST""",
        [document_id],
    ).fetchall()
    assets, findings = [], []
    for row in rows:
        exif = json.loads(row[4]) if isinstance(row[4], str) and row[4] else (row[4] or {})
        c2pa = json.loads(row[5]) if isinstance(row[5], str) and row[5] else (row[5] or {})
        appearances = conn.execute(
            "SELECT document_id, first_seen_at, context FROM image_appearances WHERE sha256 = ?",
            [row[0]],
        ).fetchall()
        locators = document_citations(conn, [a[0] for a in appearances])
        evidence = [
            {**locators.get(a[0], citation(a[0], None, None, resolved=False)),
             "first_seen_at": a[1], "context": a[2], "asset_sha256": row[0]}
            for a in appearances
        ]
        asset = {
            "sha256": row[0], "path": row[1], "mime": row[2], "phash": row[3],
            "exif": exif, "exif_verified": False, "c2pa": c2pa,
            "c2pa_status": c2pa.get("status", "not_checked") if isinstance(c2pa, dict) else "not_checked",
            "appearances": evidence,
        }
        assets.append(asset)
        distinct = {a[0] for a in appearances}
        if len(distinct) > 1:
            findings.append({
                "kind": "image_reuse", "severity": "review",
                "asset_sha256": row[0], "distinct_document_count": len(distinct),
                "evidence": evidence,
            })
        if asset["c2pa_status"] == "invalid":
            findings.append({
                "kind": "invalid_content_credentials", "severity": "high",
                "asset_sha256": row[0], "evidence": evidence,
            })
    return assets, findings


def _cross_modal(conn, doc: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    try:
        from src.analytics.cross_modal import find_intra_document_contradictions

        result = find_intra_document_contradictions(conn, doc["document_id"])
    except Exception as exc:
        result = analytic_envelope(n=0, method="cross-modal check unavailable",
                                   assumptions=ASSUMPTIONS, findings=[], error_detail=str(exc))
    findings = []
    base = citation(doc["document_id"], doc.get("source_id"), doc.get("url"), resolved=True)
    for item in result.get("findings", []):
        findings.append({
            "kind": "cross_modal_contradiction", "severity": "review",
            "detail": item,
            "evidence": [base],
        })
    return result, findings


def document_integrity(conn, document_id: str) -> dict[str, Any]:
    """One document's complete integrity history and evidence locators."""
    doc = _document(conn, document_id)
    if doc is None:
        return analytic_envelope(
            n=0, method=METHOD, assumptions=ASSUMPTIONS,
            document_id=document_id, status="not_found", findings=[],
        )
    snapshots = _snapshots(conn, doc)
    revisions, revision_findings = _revisions(conn, doc)
    assets, asset_findings = _assets(conn, document_id)
    cross_modal, cross_findings = _cross_modal(conn, doc)
    findings = [*revision_findings, *asset_findings, *cross_findings]
    return analytic_envelope(
        n=len(findings), method=METHOD, assumptions=ASSUMPTIONS,
        document={**doc, "evidence": citation(
            doc["document_id"], doc.get("source_id"), doc.get("url"), resolved=True
        )},
        status="findings" if findings else "no_findings",
        snapshots=snapshots, revisions=revisions, assets=assets,
        cross_modal=cross_modal, findings=findings,
    )


def integrity_ledger(
    conn,
    document_ids: Optional[Iterable[str]] = None,
    *,
    since_ms: Optional[int] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Aggregate document views, optionally restricted by ids/time."""
    ids = list(dict.fromkeys(document_ids or []))
    if not ids and _table(conn, "documents"):
        where, params = "", []
        if since_ms is not None:
            where, params = "WHERE ingested_at >= ?", [since_ms]
        params.append(max(1, min(int(limit), 1000)))
        ids = [row[0] for row in conn.execute(
            f"SELECT document_id FROM documents {where} ORDER BY ingested_at DESC LIMIT ?",
            params,
        ).fetchall()]
    views = [document_integrity(conn, document_id) for document_id in ids[:limit]]
    findings = [
        {"document_id": view.get("document", {}).get("document_id"), **finding}
        for view in views for finding in view.get("findings", [])
    ]
    return analytic_envelope(
        n=len(views), method=METHOD, assumptions=ASSUMPTIONS,
        documents=views, document_count=len(views), findings=findings,
        finding_count=len(findings),
    )
