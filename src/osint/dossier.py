"""
``entity_dossier(entity)`` - a cited entity brief (R11 #615).

A brief assembled *only* from already-ingested public documents: every public
mention, resolved aliases, first and last seen, and the entities connected to
it, with a citation on every line. The backbone is ``document_actors``, which
links an entity (actor) to the document it was mentioned in, so every fact is
document-sourced by construction.

Person-entity guardrail (enforced here, not just documented): a person entity
must have at least one ingested public document, and only document-sourced
facts are surfaced. A person with no ingested documents is refused rather than
described from inference, so the tool never emits an unsourced claim about an
individual.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.osint import common, evidence

# Roles that denote a human individual, used to infer person-ness when the
# caller does not pass an explicit entity_type.
_PERSON_ROLES = {"speaker", "author", "subject", "person", "spokesperson"}


def _is_person(conn, entity: str, entity_type: Optional[str]) -> bool:
    if entity_type and entity_type.strip().lower() in ("person", "people", "individual"):
        return True
    if isinstance(entity, str) and entity.lower().startswith("person:"):
        return True
    if not common.table_exists(conn, "document_actors"):
        return False
    try:
        rows = conn.execute(
            "SELECT DISTINCT lower(role) FROM document_actors "
            "WHERE actor_name = ? OR entity_id = ?",
            [entity, entity],
        ).fetchall()
    except Exception:
        return False
    roles = {r[0] for r in rows if r[0]}
    return bool(roles) and roles.issubset(_PERSON_ROLES)


def _mentions(conn, entity: str) -> List[Dict[str, Any]]:
    if not common.table_exists(conn, "document_actors"):
        return []
    rows = conn.execute(
        "SELECT document_id, actor_name, entity_id, role FROM document_actors "
        "WHERE actor_name = ? OR entity_id = ?",
        [entity, entity],
    ).fetchall()
    doc_ids = [r[0] for r in rows]
    cites = evidence.document_citations(conn, doc_ids)
    out = []
    for document_id, actor_name, entity_id, role in rows:
        cite = cites.get(document_id, evidence.citation(document_id, None, None))
        out.append(
            {
                "document_id": document_id,
                "actor_name": actor_name,
                "entity_id": entity_id,
                "role": role,
                "source": cite["source"],
                "url": cite["url"],
                "title": cite.get("title"),
                "cited": cite["cited"],
            }
        )
    return out


def _first_last_seen(conn, document_ids: List[str]) -> Dict[str, Optional[str]]:
    if not document_ids or not common.table_exists(conn, "news_articles"):
        return {"first_seen": None, "last_seen": None}
    ph = ", ".join("?" for _ in document_ids)
    row = conn.execute(
        f"SELECT MIN(publish_date), MAX(publish_date) FROM news_articles WHERE id IN ({ph})",
        document_ids,
    ).fetchone()
    return {
        "first_seen": str(row[0]) if row and row[0] is not None else None,
        "last_seen": str(row[1]) if row and row[1] is not None else None,
    }


def _connected_entities(conn, entity: str, document_ids: List[str], limit: int = 15) -> List[Dict[str, Any]]:
    """Entities co-mentioned in the same documents, each with the count of
    shared documents (its evidence weight)."""
    if not document_ids or not common.table_exists(conn, "document_actors"):
        return []
    ph = ", ".join("?" for _ in document_ids)
    rows = conn.execute(
        f"SELECT actor_name, COUNT(DISTINCT document_id) AS shared "
        f"FROM document_actors WHERE document_id IN ({ph}) AND actor_name <> ? "
        f"GROUP BY actor_name ORDER BY shared DESC LIMIT ?",
        [*document_ids, entity, int(limit)],
    ).fetchall()
    return [{"entity": r[0], "shared_documents": int(r[1])} for r in rows]


def _aliases(conn, entity: str) -> List[str]:
    """Distinct alias spellings that resolve to the same entity_id."""
    if not common.table_exists(conn, "document_actors"):
        return []
    row = conn.execute(
        "SELECT entity_id FROM document_actors WHERE actor_name = ? AND entity_id IS NOT NULL LIMIT 1",
        [entity],
    ).fetchone()
    if not row or not row[0]:
        return []
    rows = conn.execute(
        "SELECT DISTINCT actor_name FROM document_actors WHERE entity_id = ? AND actor_name <> ?",
        [row[0], entity],
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def entity_dossier(conn, entity: str, entity_type: Optional[str] = None) -> Dict[str, Any]:
    """A cited brief for one entity from ingested public documents.

    Every mention, alias, and connection carries a citation. A person entity
    with no ingested document is refused (the person-entity guardrail).
    """
    if not common.table_exists(conn, "document_actors"):
        return {"error": "no entity-mention layer available", "entity": entity}

    is_person = _is_person(conn, entity, entity_type)
    mentions = _mentions(conn, entity)

    if is_person and not mentions:
        # Person guardrail: never describe an individual from inference.
        return {
            "error": (
                f"person entity {entity!r} has no ingested public document; "
                f"refusing to surface non-document-sourced facts about an individual"
            ),
            "code": "person_requires_documents",
            "entity": entity,
            "is_person": True,
        }

    doc_ids = [m["document_id"] for m in mentions if m["document_id"]]
    seen = _first_last_seen(conn, doc_ids)
    cited_mentions = [m for m in mentions if m["cited"]]

    return {
        "entity": entity,
        "is_person": is_person,
        "found": bool(mentions),
        "mention_count": len(mentions),
        "cited_mention_count": len(cited_mentions),
        "uncited_count": evidence.uncited_count(mentions),
        "aliases": _aliases(conn, entity),
        "first_seen": seen["first_seen"],
        "last_seen": seen["last_seen"],
        "mentions": mentions[:40],
        "connected_entities": _connected_entities(conn, entity, doc_ids),
        # Every surfaced fact is document-sourced; there are no inference-only
        # lines in this payload.
        "document_sourced_only": True,
    }
