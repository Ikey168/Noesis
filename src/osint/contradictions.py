"""
``contradiction_scan(entity|topic)`` - the public record vs itself (R10 #613).

Surfaces where already-ingested public documents disagree, from the semantic
conflict edges (``claim_conflicts``, the CONTRADICTS relation). Pure
composition: it joins each conflicting claim back to its text, source and
document so every entry is cited. An entry whose document cannot be resolved to
a citation is *flagged* ``cited: false``, never dropped, so gaps are visible
rather than hidden.

Scoped by ``topic`` (the conflict table's topic column, or a substring of the
claim text) or by ``entity`` (a substring of either claim's text). Stdlib-only;
the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.osint import common


def contradiction_scan(
    conn,
    topic: Optional[str] = None,
    entity: Optional[str] = None,
    limit: int = 40,
) -> Dict[str, Any]:
    """A cited contradiction ledger for a topic or entity.

    Returns each contradiction pair with both claims (text, source, citation)
    and whether it is cited. ``uncited_count`` reports how many entries lack a
    resolvable citation, so the gap is legible.
    """
    if not common.table_exists(conn, "claim_conflicts"):
        return {"contradictions": [], "count": 0, "topic": topic, "entity": entity,
                "note": "no conflict layer available"}

    where: List[str] = ["lower(conflict_type) LIKE '%contradict%'"]
    params: List[Any] = []
    if topic:
        where.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT claim_id_a, claim_id_b, conflict_type, topic, similarity_score
        FROM claim_conflicts
        WHERE {' AND '.join(where)}
        ORDER BY similarity_score DESC NULLS LAST
        LIMIT ?
        """,
        params,
    ).fetchall()

    all_ids = list({r[0] for r in rows} | {r[1] for r in rows})
    texts = _claim_texts(conn, all_ids)
    sources = common.claim_sources(conn, all_ids)

    contradictions = []
    uncited = 0
    for r in rows:
        a = _claim_view(r[0], texts, sources)
        b = _claim_view(r[1], texts, sources)
        # Entity filter: keep pairs where either claim mentions the entity.
        if entity:
            hay = f"{a['text']} {b['text']}".lower()
            if entity.lower() not in hay:
                continue
        cited = bool(a["cited"] and b["cited"])
        if not cited:
            uncited += 1
        contradictions.append(
            {
                "claim_a": a,
                "claim_b": b,
                "conflict_type": r[2],
                "topic": r[3],
                "similarity": float(r[4]) if r[4] is not None else None,
                "cited": cited,
            }
        )

    return {
        "contradictions": contradictions,
        "count": len(contradictions),
        "uncited_count": uncited,
        "topic": topic,
        "entity": entity,
    }


def _claim_texts(conn, claim_ids: List[str]) -> Dict[str, str]:
    if not claim_ids or not common.table_exists(conn, "argument_claims"):
        return {}
    ph = ", ".join("?" for _ in claim_ids)
    rows = conn.execute(
        f"SELECT claim_id, claim_text FROM argument_claims WHERE claim_id IN ({ph})",
        claim_ids,
    ).fetchall()
    return {r[0]: r[1] or "" for r in rows}


def _claim_view(claim_id: str, texts: Dict[str, str], sources: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    src = sources.get(claim_id, {})
    document_id = src.get("document_id")
    url = src.get("url")
    return {
        "claim_id": claim_id,
        "text": (texts.get(claim_id, "") or "")[:220],
        "source": src.get("source", "unknown"),
        "source_type": src.get("source_type"),
        "document_id": document_id,
        "url": url,
        # An entry is cited when its claim resolves to a real corpus document;
        # a dangling document_id is flagged, not hidden.
        "cited": bool(src.get("resolved")),
    }
