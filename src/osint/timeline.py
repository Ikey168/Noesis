"""
``timeline_reconstruct(topic|entity)`` - a cited event sequence (R11 #617).

Reconstructs a sequence of events from dated, cited claims (``argument_claims``
joined to ``news_articles`` for the publish date and citation), scoped by topic
(a substring of the claim text) or entity (an actor in ``document_actors``).
Claims are bucketed by day into events; each event reports its **corroboration
density**, the number of independent sources that back it, so a well-sourced
event is visibly distinct from a single-sourced one.

Every entry is cited; an entry whose document does not resolve is flagged, not
hidden. Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.osint import common, evidence


def _entity_document_ids(conn, entity: str) -> List[str]:
    if not common.table_exists(conn, "document_actors"):
        return []
    rows = conn.execute(
        "SELECT DISTINCT document_id FROM document_actors "
        "WHERE actor_name = ? OR entity_id = ?",
        [entity, entity],
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def timeline_reconstruct(
    conn,
    topic: Optional[str] = None,
    entity: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """A day-bucketed event timeline of cited claims for a topic or entity,
    each event carrying its corroboration density (independent-source count)."""
    if not common.table_exists(conn, "argument_claims"):
        return {"events": [], "count": 0, "topic": topic, "entity": entity,
                "note": "no claim layer available"}

    where: List[str] = []
    params: List[Any] = []
    if topic:
        where.append("c.claim_text ILIKE ?")
        params.append(f"%{topic}%")
    if entity:
        doc_ids = _entity_document_ids(conn, entity)
        if not doc_ids:
            return {"events": [], "count": 0, "topic": topic, "entity": entity,
                    "note": f"entity {entity!r} not found in the corpus"}
        where.append("c.document_id IN (" + ", ".join("?" for _ in doc_ids) + ")")
        params.extend(doc_ids)

    citation_tbl = common.citation_table(conn)
    has_articles = citation_tbl is not None
    date_expr = "a.publish_date" if has_articles else "CAST(NULL AS TIMESTAMP)"
    join = f"LEFT JOIN {citation_tbl} a ON c.document_id = a.id" if has_articles else ""
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))

    rows = conn.execute(
        f"""
        SELECT c.claim_id, c.claim_text, c.document_id, {date_expr} AS dt,
               {'a.source' if has_articles else 'NULL'} AS source,
               {'a.url' if has_articles else 'NULL'} AS url
        FROM argument_claims c
        {join}
        {clause}
        ORDER BY dt NULLS LAST
        LIMIT ?
        """,
        params,
    ).fetchall()

    # Bucket by calendar day; each bucket is one event.
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for claim_id, text, document_id, dt, source, url in rows:
        day = str(dt)[:10] if dt is not None else "undated"
        if day not in buckets:
            buckets[day] = []
            order.append(day)
        cite = evidence.citation(document_id, source, url, resolved=source is not None)
        buckets[day].append(
            {
                "claim_id": claim_id,
                "text": (text or "")[:200],
                "source": cite["source"],
                "url": cite["url"],
                "path": cite["path"],
                "cited": cite["cited"],
            }
        )

    events = []
    for day in order:
        entries = buckets[day]
        independent = len({e["source"] for e in entries if e["cited"] and e["source"] != "unknown"})
        events.append(
            {
                "date": day,
                "entries": entries,
                "claim_count": len(entries),
                "corroboration_density": independent,
                "state": evidence.render_state(independent),
                "uncited_count": evidence.uncited_count(entries),
            }
        )

    return {
        "events": events,
        "count": len(events),
        "claim_count": sum(len(b) for b in buckets.values()),
        "topic": topic,
        "entity": entity,
    }
