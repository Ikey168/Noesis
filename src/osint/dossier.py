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
individual. Person-ness is classified fail-closed by
:func:`src.osint.common.is_person`: an entity that cannot be confidently
classified as non-human (no explicit ``entity_type``, no roles) is treated as a
person, so the guardrail errs toward refusal.

OX02 (#2042): for an organization entity, an optional ``ownership`` section
composes the Corporate Ownership bundle's record owners (``src.kb.ownership_*``)
instead of duplicating them. It is assembled only when the caller requests it
and the bundle is enabled in the namespace
(:func:`src.kb.ownership_bundle.is_enabled`). The entity is resolved to
ownership records only through an exact ownership record key, the ownership
canonical entity id, or an accepted, unreverted identity decision; never by name
similarity. Ambiguous resolution returns candidates; conflicting assertions are
shown side by side. Every line cites the ownership record revision it came
from. It is never assembled for a person entity.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.osint import common, evidence


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
    citation_tbl = common.citation_table(conn)
    if not document_ids or not citation_tbl:
        return {"first_seen": None, "last_seen": None}
    ph = ", ".join("?" for _ in document_ids)
    row = conn.execute(
        f"SELECT MIN(publish_date), MAX(publish_date) FROM {citation_tbl} WHERE id IN ({ph})",
        document_ids,
    ).fetchone()
    return {
        "first_seen": str(row[0]) if row and row[0] is not None else None,
        "last_seen": str(row[1]) if row and row[1] is not None else None,
    }


def _connected_entities(
    conn, entity: str, document_ids: List[str], limit: int = 15
) -> List[Dict[str, Any]]:
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


OWNERSHIP_NOTICE = (
    "Ownership lines report what each registry source states, as of the date shown; conflicts are "
    "shown side by side, never merged. No beneficial-ownership, sanctions or AML determination is made."
)


def _entity_ids(conn, entity: str) -> List[str]:
    ids = [entity]
    if common.table_exists(conn, "document_actors"):
        rows = conn.execute(
            "SELECT DISTINCT entity_id FROM document_actors "
            "WHERE (actor_name = ? OR entity_id = ?) AND entity_id IS NOT NULL",
            [entity, entity],
        ).fetchall()
        ids += [r[0] for r in rows if r[0]]
    return list(dict.fromkeys(ids))


def _accepted_matches(conn, namespace: str, ids: Iterable[str]) -> List[Dict[str, Any]]:
    """Accepted, unreverted ``match`` identity decisions naming one of *ids*."""
    if not common.table_exists(conn, "entity_identity_decisions"):
        return []
    wanted = set(ids)
    rows = conn.execute(
        "SELECT decision_id, decision_type, subject_ids_json, payload_json "
        "FROM entity_identity_decisions WHERE namespace = ?",
        [namespace],
    ).fetchall()
    undone = set()
    for _did, dtype, _subjects, payload in rows:
        if dtype == "undo":
            undone.add((json.loads(payload or "{}").get("payload") or {}).get("undoes"))
    out = []
    for did, dtype, subjects, _payload in rows:
        subject_ids = json.loads(subjects or "[]")
        if dtype == "match" and did not in undone and wanted & set(subject_ids):
            out.append({"decision_id": did, "subject_ids": subject_ids})
    return out


def _cite_ownership(item: Mapping[str, Any]) -> Dict[str, Any]:
    source = item.get("source") or {}
    return {
        "record_id": item.get("record_id"),
        "revision": item.get("revision"),
        "provider": source.get("provider"),
        "publisher": source.get("publisher"),
        "url": source.get("url"),
        "locator": source.get("locator") or source.get("provider_record_id"),
        "cited": bool(item.get("record_id")),
    }


def _resolve_ownership(
    conn, graph, namespace: str, entity: str
) -> List[Dict[str, Any]]:
    from src.kb.ownership_store import canonical_entity_id

    ids = _entity_ids(conn, entity)
    by_canonical = {canonical_entity_id(key): key for key in graph.entities}
    hits: List[Dict[str, Any]] = []
    for value in ids:
        if value in graph.entities:
            hits.append(
                {"record_key": value, "basis": "ownership-record-key", "via": value}
            )
        elif value in by_canonical:
            hits.append(
                {
                    "record_key": by_canonical[value],
                    "basis": "ownership-canonical-entity",
                    "via": value,
                }
            )
    for decision in _accepted_matches(conn, namespace, ids):
        for subject in decision["subject_ids"]:
            key = by_canonical.get(subject) or (
                subject if subject in graph.entities else None
            )
            if key:
                hits.append(
                    {
                        "record_key": key,
                        "basis": "accepted-identity-decision",
                        "via": decision["decision_id"],
                    }
                )
    return hits


def _entity_lines(graph, keys: Iterable[str]) -> List[Dict[str, Any]]:
    lines = []
    for key in sorted(keys):
        for view in graph.entities.get(key, []):
            if view.get("redacted"):
                continue
            body = view["record"]
            lines.append(
                {
                    "record_key": key,
                    "name": body.get("name"),
                    "jurisdiction": body.get("jurisdiction"),
                    "identifiers": body.get("identifiers") or [],
                    "status": body.get("status"),
                    "citation": _cite_ownership({**view, "source": body.get("source")}),
                }
            )
    return lines


def _jurisdiction(graph, key: Optional[str]) -> Optional[str]:
    for view in graph.entities.get(key or "", []):
        if not view.get("redacted") and view["record"].get("jurisdiction"):
            return view["record"]["jurisdiction"]
    return None


def _edge_line(graph, edge: Mapping[str, Any], counterpart: str) -> Dict[str, Any]:
    holder = edge.get("holder") or {}
    return {
        "counterpart": counterpart,
        "assertion_kind": edge.get("assertion_kind"),
        "holder_name": holder.get("name"),
        "share": edge.get("share"),
        "validity": edge.get("validity"),
        "as_of_status": edge.get("as_of_status"),
        "jurisdiction": _jurisdiction(graph, edge.get("subject_key")),
        "citation": _cite_ownership(edge),
    }


def _ownership_section(
    conn, entity: str, is_person: bool, request: Mapping[str, Any]
) -> Dict[str, Any]:
    namespace = str(request.get("namespace") or "")
    as_of = request.get("as_of") or date.today().isoformat()
    base = {
        "feature": "ownership",
        "namespace": namespace,
        "as_of": as_of,
        "notice": OWNERSHIP_NOTICE,
    }
    if is_person:
        return {
            **base,
            "status": "not_assembled_for_person",
            "note": "the ownership section is never assembled for a person entity",
        }
    if not common.table_exists(conn, "ownership_records"):
        return {
            **base,
            "status": "inert",
            "reason": "no Corporate Ownership records in this warehouse",
        }
    from src.kb import ownership_bundle
    from src.kb.ownership_graph import OwnershipGraph, query
    from src.kb.ownership_store import OwnershipError

    if not ownership_bundle.is_enabled(conn, namespace):
        return {
            **base,
            "status": "inert",
            "reason": "Corporate Ownership is not enabled in this namespace",
        }
    principal, scopes = request.get("principal_id"), set(request.get("scopes") or ())
    try:
        graph = OwnershipGraph(conn, namespace, principal_id=principal, scopes=scopes)
    except OwnershipError as exc:
        return {**base, "status": exc.code, "reason": str(exc)}
    hits = _resolve_ownership(conn, graph, namespace, entity)
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for hit in hits:
        clusters.setdefault(graph.cluster(hit["record_key"]), []).append(hit)
    if not clusters:
        return {
            **base,
            "status": "not_resolved",
            "note": "no ownership record key, ownership entity id or accepted identity decision links this "
            "entity to ownership records; names are never matched",
        }
    if len(clusters) > 1:
        return {
            **base,
            "status": "needs_selection",
            "candidates": [
                {**graph.describe(root), "resolved_by": found}
                for root, found in sorted(clusters.items())
            ],
            "note": "the entity resolves to more than one ownership entity; review the identity decisions. "
            "Noesis never picks.",
        }
    root, resolved_by = next(iter(clusters.items()))
    members = graph.members(root)

    def run(name: str) -> Dict[str, Any]:
        return query(
            conn,
            namespace,
            name,
            root,
            principal_id=principal,
            scopes=scopes,
            as_of=as_of,
        )

    parents, ultimate, subsidiaries = (
        run("direct_parents"),
        run("ultimate_parents"),
        run("subsidiaries"),
    )

    def slot_lines(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "holder": group["holder"],
                "assertions": [
                    _edge_line(graph, a, group["holder"]) for a in group["assertions"]
                ],
                "differences": group["differences"],
            }
            for group in result["groups"]
        ]

    officers = []
    for view in graph.views:
        body = view["record"]
        if (
            body["kind"] != "officer_role"
            or body.get("entity_key") not in members
            or view.get("redacted")
        ):
            continue
        resigned = body.get("resigned_on")
        officers.append(
            {
                "officer": body.get("officer"),
                "role": body.get("role"),
                "appointed_on": body.get("appointed_on"),
                "resigned_on": resigned,
                "current_as_of": not (resigned and str(resigned) <= as_of),
                "jurisdiction": _jurisdiction(graph, body.get("entity_key")),
                "citation": _cite_ownership({**view, "source": body.get("source")}),
                "note": "as stated in the ownership record; not profiled further",
            }
        )
    conflicts = [{"slot": "direct_parents", **c} for c in parents["conflicts"]] + [
        {"slot": "ultimate_parents", **c} for c in ultimate["conflicts"]
    ]
    return {
        **base,
        "status": "assembled",
        "resolution": {"root": root, "members": members, "resolved_by": resolved_by},
        "identity": _entity_lines(graph, members),
        "direct_parents": slot_lines(parents),
        "ultimate_parents": slot_lines(ultimate),
        "subsidiaries": [
            {
                "subject": s["subject"],
                "assertions": [
                    _edge_line(graph, a, s["subject"]) for a in s["assertions"]
                ],
            }
            for s in subsidiaries["subsidiaries"]
        ],
        "reporting_exceptions": [
            _edge_line(graph, e, root)
            for e in parents["reporting_exceptions"] + ultimate["reporting_exceptions"]
        ],
        "officers": sorted(
            officers,
            key=lambda o: (o["appointed_on"] or "", o["citation"]["record_id"] or ""),
        ),
        "conflicts": conflicts,
        "pins": parents.get("pins"),
    }


def entity_dossier(
    conn,
    entity: str,
    entity_type: Optional[str] = None,
    *,
    ownership: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """A cited brief for one entity from ingested public documents.

    Every mention, alias, and connection carries a citation. A person entity
    with no ingested document is refused (the person-entity guardrail).

    ``ownership`` (optional feature, off by default) requests the Corporate
    Ownership section: ``{"namespace", "principal_id", "scopes", "as_of"?}``.
    """
    if not common.table_exists(conn, "document_actors"):
        return {"error": "no entity-mention layer available", "entity": entity}

    is_person = common.is_person(conn, entity, entity_type)
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

    out = {
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
    if ownership is not None:
        # Registry-sourced, cited to ownership record revisions; kept apart from
        # the document-sourced lines above.
        out["ownership"] = _ownership_section(conn, entity, is_person, ownership)
    return out
