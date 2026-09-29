"""
Image reuse detection (Track C / C2).

The most common image failure in open sources is not fakery but **recycling** —
a real photo attached to the wrong event, place, or year. This module clusters
stored image assets by perceptual-hash distance (C1's dHash) and flags clusters
whose near-duplicate images appear across **multiple distinct documents**: a
reuse finding, citing every appearance.

Findings follow the statistical-honesty contract (``n`` = appearances, method,
the hash-distance threshold as a declared assumption) and the OSINT evidence
discipline (every appearance is a citation). A high-confidence conflicting reuse
is a ledger entry; below the confidence bar it stays a flagged suggestion.

Reads ``image_assets`` + ``image_appearances`` from an injected connection, so
the read-only warehouse (the MCP server) and a writable store both work.

Video (OX04, #2044): keyframes sampled by the media connector are assets too.
A frame appearance cites its media document plus the offset in seconds and is
labelled ``video_frame`` apart from ``still_image`` appearances; a video counts
as one document for the "spans multiple documents" rule, however many of its
frames match.

See ``docs/architecture/OSINT_IMAGERY_PLAN.md`` §3.2.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope
from src.ingestion.assets.provenance import hamming_distance

DEFAULT_MAX_DISTANCE = 6  # dHash bits; ~<=6/64 is a robust near-duplicate
MAX_ASSETS = 2000  # clustering is O(n^2); cap and report if exceeded

METHOD = "perceptual-hash (dHash) near-duplicate clustering"


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def _assumptions(max_distance: int, extra: Optional[List[str]] = None) -> List[str]:
    base = [
        f"near-duplicate threshold = {max_distance} dHash bits (of 64)",
        "EXIF and appearance context are file-claimed, not verified",
        "a reuse finding flags recycling, not fakery; review each before acting",
    ]
    return base + (extra or [])


def _load_assets(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT sha256, phash FROM image_assets WHERE phash IS NOT NULL ORDER BY sha256 LIMIT ?",
        [MAX_ASSETS + 1],
    ).fetchall()
    return [{"sha256": r[0], "phash": r[1]} for r in rows]


def _appearances(conn, sha256: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT document_id, first_seen_at, context FROM image_appearances WHERE sha256 = ? ORDER BY first_seen_at NULLS LAST, document_id",
        [sha256],
    ).fetchall()
    from src.ingestion.assets.store import parse_frame_context

    out = []
    for document_id, first_seen_at, context in rows:
        frame = parse_frame_context(context)
        entry = {"document_id": document_id, "first_seen_at": first_seen_at, "context": context, "cited": True}
        if frame is None:
            entry.update(appearance_kind="still_image", citation={"document_id": document_id})
        else:
            # OX04: a sampled video frame cites its media document plus the offset.
            entry.update(
                appearance_kind="video_frame",
                offset_s=frame.get("offset_s"),
                scene_index=frame.get("scene_index"),
                citation={
                    "document_id": document_id,
                    "offset_s": frame.get("offset_s"),
                    "media_fragment": frame.get("media_fragment"),
                },
            )
        out.append(entry)
    return out


def _cluster(assets: List[Dict[str, Any]], max_distance: int) -> List[List[int]]:
    """Union-find clustering by pairwise pHash hamming distance <= threshold."""
    n = len(assets)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            dist = hamming_distance(assets[i]["phash"], assets[j]["phash"])
            if dist is not None and dist <= max_distance:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def find_reuse(conn, max_distance: int = DEFAULT_MAX_DISTANCE, topic: Optional[str] = None) -> Dict[str, Any]:
    """Reuse findings: near-duplicate image clusters spanning >=2 documents.

    Each finding carries its assets, every appearance (cited), and the count of
    distinct documents it spans. A finding is ``conflicting`` (ledger-worthy)
    when it spans distinct documents; confidence scales with distinctness.
    """
    if not _table_exists(conn, "image_assets") or not _table_exists(conn, "image_appearances"):
        return analytic_envelope(n=0, method=METHOD, assumptions=_assumptions(max_distance), findings=[], note="no image provenance available")

    assets = _load_assets(conn)
    truncated = len(assets) > MAX_ASSETS
    assets = assets[:MAX_ASSETS]
    clusters = _cluster(assets, max_distance)

    findings: List[Dict[str, Any]] = []
    total_appearances = 0
    for group in clusters:
        members = [assets[i] for i in group]
        appearances: List[Dict[str, Any]] = []
        for m in members:
            appearances.extend(_appearances(conn, m["sha256"]))
        distinct_docs = sorted({a["document_id"] for a in appearances})
        if len(distinct_docs) < 2:
            continue  # not reused across documents
        if topic and not any(topic.lower() in (a.get("context") or "").lower() for a in appearances):
            continue
        total_appearances += len(appearances)
        frame_docs = sorted({a["document_id"] for a in appearances if a["appearance_kind"] == "video_frame"})
        findings.append({
            "asset_shas": [m["sha256"] for m in members],
            "video_documents": frame_docs,
            "includes_video_frames": bool(frame_docs),
            "distinct_document_count": len(distinct_docs),
            "documents": distinct_docs,
            "appearances": appearances,
            "conflicting": True,
            "confidence": "high" if len(distinct_docs) >= 3 else "medium",
        })

    findings.sort(key=lambda f: f["distinct_document_count"], reverse=True)
    extra = ["asset cap exceeded; clustering limited"] if truncated else None
    return analytic_envelope(
        n=total_appearances,
        method=METHOD,
        assumptions=_assumptions(max_distance, extra),
        findings=findings,
        finding_count=len(findings),
        truncated=truncated,
    )


def image_provenance(conn, sha256: str) -> Dict[str, Any]:
    """Provenance for one asset: metadata claims (EXIF/pHash/C2PA) + appearances."""
    if not _table_exists(conn, "image_assets"):
        return {"error": "no image provenance available"}
    import json

    row = conn.execute(
        "SELECT sha256, mime, width, height, phash, exif, c2pa FROM image_assets WHERE sha256 = ?",
        [sha256],
    ).fetchone()
    if row is None:
        return {"error": f"asset not found: {sha256}"}
    exif = json.loads(row[5]) if isinstance(row[5], str) else row[5]
    c2pa = json.loads(row[6]) if isinstance(row[6], str) else row[6]
    return {
        "sha256": row[0],
        "mime": row[1],
        "width": row[2],
        "height": row[3],
        "phash": row[4],
        "exif": exif or {},
        "exif_note": "EXIF is claimed by the file, not verified",
        "c2pa": c2pa,
        "appearances": _appearances(conn, sha256),
    }


def image_reuse(conn, sha256: str, max_distance: int = DEFAULT_MAX_DISTANCE) -> Dict[str, Any]:
    """Near-duplicates of one asset and where they appear (honesty-enveloped)."""
    if not _table_exists(conn, "image_assets"):
        return analytic_envelope(n=0, method=METHOD, assumptions=_assumptions(max_distance), near_duplicates=[])
    target = conn.execute("SELECT phash FROM image_assets WHERE sha256 = ?", [sha256]).fetchone()
    if target is None or target[0] is None:
        return analytic_envelope(n=0, method=METHOD, assumptions=_assumptions(max_distance), near_duplicates=[], note="asset unknown or unhashed")
    target_hash = target[0]
    near: List[Dict[str, Any]] = []
    appearances_total = 0
    for asset in _load_assets(conn):
        if asset["sha256"] == sha256:
            continue
        dist = hamming_distance(target_hash, asset["phash"])
        if dist is not None and dist <= max_distance:
            apps = _appearances(conn, asset["sha256"])
            appearances_total += len(apps)
            near.append({"sha256": asset["sha256"], "distance": dist, "appearances": apps})
    near.sort(key=lambda x: x["distance"])
    return analytic_envelope(
        n=appearances_total,
        method=METHOD,
        assumptions=_assumptions(max_distance),
        sha256=sha256,
        near_duplicates=near,
        near_duplicate_count=len(near),
    )
