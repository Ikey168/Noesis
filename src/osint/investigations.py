"""
Investigations as provisioned KGs, plus the review gate (R11 #618).

An "investigation" is not a new abstraction: it is a Track P-provisioned,
namespaced knowledge graph (R8) fed by a chosen source set. Because every
provisioning action (deploy / attach / ingest / teardown) is already written to
the provisioning lineage log, an investigation is fully reconstructable from
its audit trail. :func:`investigation_audit` replays that trail.

The review gate: ``geolocate_claims`` and ``narrative_coordination`` are the
most abusable and false-positive-prone tools. They stay behind an explicit
review gate: they are **absent** from the served tool surface until the gate
documented in ``docs/security/osint-review-gate.md`` passes. :data:`GATED_TOOLS` names
them so a test can assert they are not exposed.

:func:`osint_telemetry` supplies the empty-canvas ambient signal when the OSINT
surface dominates: open investigation threads, newly corroborated and newly
contradicted claims.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.osint import common

# Tools held behind the explicit review gate; absent from the server until the
# gate passes. Named here so enforcement is testable.
GATED_TOOLS = (
    "geolocate_claims",
    "narrative_coordination",
    "reverse_image_search",
    "geolocate_image",
)


def is_gated(tool_name: str) -> bool:
    """True if a tool is behind the review gate (must not be served yet)."""
    return tool_name in GATED_TOOLS


def investigation_audit(conn, name: str) -> Dict[str, Any]:
    """Reconstruct an investigation from its provisioning audit trail: the KG
    record, its bound sources, and every logged provisioning action in order,
    so the investigation can be fully replayed."""
    from src.provisioning import store

    kg = store.get_kg(conn, name)
    if kg is None:
        return {"error": f"investigation {name!r} not found", "code": "not_found"}
    events = list(reversed(store.list_events(conn, name, limit=500)))  # oldest first
    return {
        "investigation": name,
        "kg": kg,
        "sources": store.list_sources(conn, name),
        "audit_trail": events,
        "action_count": len(events),
        "reconstructable": True,
    }


def list_investigations(conn) -> Dict[str, Any]:
    """Deployed investigations (provisioned KGs) with their audit-action counts."""
    from src.provisioning import store

    out = []
    for kg in store.list_kgs(conn, include_archived=False):
        events = store.list_events(conn, kg["name"], limit=500)
        out.append(
            {
                "name": kg["name"],
                "description": kg["description"],
                "source_count": store.count_sources(conn, kg["name"]),
                "action_count": len(events),
            }
        )
    return {"investigations": out, "count": len(out)}


def osint_telemetry(conn) -> Dict[str, Any]:
    """Empty-canvas ambient signal for an OSINT-dominant canvas: open threads,
    newly corroborated, newly contradicted. Returns ``{}`` when there is no
    OSINT activity so the engine falls back to the library signal."""
    threads = list_investigations(conn)["investigations"]
    corroborated = 0
    if common.table_exists(conn, "claim_evidence"):
        corroborated = int(
            conn.execute(
                "SELECT COUNT(*) FROM claim_evidence WHERE lower(relation) = 'supports'"
            ).fetchone()[0]
        )
    contradicted = 0
    contradiction_topics: List[str] = []
    if common.table_exists(conn, "claim_conflicts"):
        contradicted = int(
            conn.execute(
                "SELECT COUNT(*) FROM claim_conflicts WHERE lower(conflict_type) LIKE '%contradict%'"
            ).fetchone()[0]
        )
        contradiction_topics = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT topic FROM claim_conflicts "
                "WHERE topic IS NOT NULL AND lower(conflict_type) LIKE '%contradict%' LIMIT 6"
            ).fetchall()
            if r[0]
        ]

    if not (threads or corroborated or contradicted):
        return {}

    return {
        "signals": [
            {"label": "OPEN THREADS", "value": len(threads)},
            {"label": "CORROBORATED", "value": corroborated},
            {"label": "CONTRADICTED", "value": contradicted},
        ],
        "movers": [
            {
                "label": f"work thread {t['name']}",
                "intent": f"investigation {t['name']} dossier and timeline",
                "change": t["action_count"],
            }
            for t in threads[:5]
        ],
        "ticker": {
            "label": "NEWLY CONTRADICTED",
            "items": contradiction_topics,
        } if contradiction_topics else None,
    }
