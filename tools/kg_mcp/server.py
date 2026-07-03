"""
NeuroNews Knowledge-Graph inspector — MCP server.

Token-efficient read-only access to:
  • The live in-process KnowledgeGraphStore (nodes + triples)
  • The entity-correction queue (pending / approved / rejected)
  • The KG ontology (entity types, relation types, allowed pairs)
  • Emerging-connections and evolving-topics summaries

Tools:

  kg_stats()                             -> node/triple counts + status
  kg_ontology()                          -> entity types, relation types, constraints
  list_entities(type?, name_filter?,     -> compact node list
                limit?)
  get_entity(entity_id)                  -> full node details + neighbour count
  list_corrections(status?,              -> correction queue / history
                   entity_id?, limit?)
  get_correction(correction_id)          -> single correction detail
  emerging_connections(since_minutes?,   -> new KG edges in the time window
                       limit?)
  evolving_topics(window_minutes?,       -> entities with recent edge bursts
                  top_n?)

Design constraints (same as other NeuroNews MCP servers):
  * Lazy imports inside each tool — top-level imports are stdlib + fastmcp only.
  * All results are capped summaries, never full payloads.
  * Read-only: no tool mutates the KG store or the correction store.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Stdlib-only helper for the analytics honesty contract (R6); safe at import.
from src.analytics.honesty import honesty_output_schema  # noqa: E402

mcp = FastMCP("neuronews-kg")

MAX_LIST = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_kg_store():
    from src.knowledge_graph.kg_updater import _shared_store
    return _shared_store()


def _get_correction_store():
    from src.knowledge_graph.entity_corrections import get_correction_store
    return get_correction_store()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def kg_stats() -> dict:
    """
    Return current KnowledgeGraphStore statistics.

    Fields: node_count, triple_count, total_update_events, triple_events,
    node_events, status.
    """
    from src.knowledge_graph.kg_updater import get_store_stats
    return get_store_stats()


@mcp.tool()
def kg_ontology() -> dict:
    """
    Return the KG ontology: entity types, relation types, and which
    (subject_type, object_type) pairs each relation allows.

    Use this instead of reading src/knowledge_graph/foundation/ontology.py.
    """
    from src.knowledge_graph.foundation.ontology import (
        EntityType, RelationType, allowed_pairs,
    )
    return {
        "entity_types": [e.value for e in EntityType],
        "relation_types": [r.value for r in RelationType],
        "constraints": {
            rel.value: [
                {"subject": s.value, "object": o.value}
                for s, o in sorted(allowed_pairs(rel))
            ]
            for rel in RelationType
        },
    }


@mcp.tool(
    meta={"panel": {
        "type": "entity_graph",
        "title": "Entity graph",
        "description": "Co-mention network of entities in recent coverage.",
        "endpoint": "/api/v1/entity_graph",
        "facets": ["entities", "actors", "overview"],
        "tables": ["documents"],
        "ui_flag": "influence_graph",
        "default_span": 6,
        "days_param": "days",
        "max_days": 30,
    }},
)
def list_entities(
    entity_type: Optional[str] = None,
    name_filter: Optional[str] = None,
    limit: int = MAX_LIST,
) -> list:
    """
    List nodes in the live KG store.

    Args:
        entity_type:  Filter by type (Person, Organization, Concept, …).
                      Omit to list all types.
        name_filter:  Case-insensitive substring filter on node name.
        limit:        Max results (default 50).

    Returns a list of {node_id, type, name, alias_count, property_count}.
    Use get_entity(node_id) for full details.
    """
    from src.knowledge_graph.foundation.ontology import EntityType

    store = _get_kg_store()

    if entity_type is not None:
        try:
            etype = EntityType(entity_type)
        except ValueError:
            valid = [e.value for e in EntityType]
            return [{"error": f"Unknown entity_type {entity_type!r}. Valid: {valid}"}]
        nodes = store.nodes_by_type(etype)
    else:
        nodes = list(store._nodes.values())

    if name_filter:
        nf = name_filter.lower()
        nodes = [n for n in nodes if nf in n.name.lower()]

    nodes = nodes[:limit]
    return [
        {
            "node_id": n.node_id,
            "type": n.type.value,
            "name": n.name,
            "alias_count": len(n.aliases),
            "property_count": len(n.properties),
        }
        for n in nodes
    ]


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """
    Return full details of a KG node plus its neighbour count.

    Args:
        entity_id:  The node id (e.g. ``person:4a7f2c9d1b3e``).
    """
    store = _get_kg_store()
    node = store.get_node(entity_id)
    if node is None:
        return {"error": f"Entity {entity_id!r} not found"}

    neighbours = store.neighbors(entity_id)
    return {
        **node.to_dict(),
        "neighbour_count": len(neighbours),
        "neighbours_sample": [
            {
                "predicate": t.predicate.value,
                "other_id": t.object if t.subject == entity_id else t.subject,
            }
            for t in neighbours[:5]
        ],
    }


@mcp.tool()
def list_corrections(
    status: Optional[str] = "pending",
    entity_id: Optional[str] = None,
    limit: int = MAX_LIST,
) -> list:
    """
    List entity correction requests.

    Args:
        status:     pending | approved | rejected | all  (default: pending)
        entity_id:  Filter to one entity's corrections.
        limit:      Max results (default 50).

    Returns compact correction summaries. Use get_correction(id) for details.
    """
    from src.knowledge_graph.entity_corrections import CorrectionStatus

    cs = _get_correction_store()
    status_filter = None
    if status and status != "all":
        try:
            status_filter = CorrectionStatus(status)
        except ValueError:
            return [{"error": f"Unknown status {status!r}. Valid: pending, approved, rejected, all"}]

    corrections = cs.list_corrections(entity_id=entity_id, status=status_filter, limit=limit)
    return [
        {
            "correction_id": c.correction_id,
            "entity_id": c.entity_id,
            "correction_type": c.correction_type.value,
            "status": c.status.value,
            "submitted_by": c.submitted_by,
            "submitted_at": c.submitted_at.isoformat(),
            "version": c.version,
            "reason": c.reason[:80],
        }
        for c in corrections
    ]


@mcp.tool()
def get_correction(correction_id: str) -> dict:
    """
    Return full details of a single entity correction request.

    Args:
        correction_id:  UUID of the correction.
    """
    cs = _get_correction_store()
    c = cs.get(correction_id)
    if c is None:
        return {"error": f"Correction {correction_id!r} not found"}
    return c.to_dict()


@mcp.tool()
def emerging_connections(
    since_minutes: int = 60,
    limit: int = 20,
) -> list:
    """
    Return KG edges that were added in the last ``since_minutes`` minutes.

    Each item shows subject entity, predicate, object entity, source document,
    and timestamp. Use to monitor newly discovered relationships.

    Args:
        since_minutes:  Look-back window in minutes (default 60).
        limit:          Max results (default 20).
    """
    from datetime import datetime, timezone, timedelta
    from src.knowledge_graph.kg_updater import get_emerging_connections

    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    return get_emerging_connections(since=since, limit=limit)


@mcp.tool(
    meta={"panel": {
        "type": "trending",
        "title": "Trending topics",
        "description": "Topics ranked by mention velocity.",
        "endpoint": "/topics/trending",
        "facets": ["overview", "trend", "events"],
        "tables": ["news_articles"],
        "ui_flag": "trending",
        "default_span": 6,
        "days_param": "days",
        "max_days": 30,
    }},
)
def evolving_topics(
    window_minutes: int = 60,
    top_n: int = 15,
) -> list:
    """
    Return entities ranked by how many new MENTIONS triples they received in
    the last ``window_minutes`` minutes.

    A high ``new_connections`` count signals a topic that many recently
    ingested documents reference — i.e. an actively evolving topic.

    Args:
        window_minutes:  Look-back window in minutes (default 60).
        top_n:           Maximum number of results (default 15).
    """
    from src.knowledge_graph.kg_updater import get_evolving_topics
    return get_evolving_topics(window_seconds=window_minutes * 60, top_n=top_n)


# ---------------------------------------------------------------------------
# Graph analytics (R6 / Track DS Wave 1b): communities + centrality over the
# co-mention graph. These enrich the entity_graph panel (colour + size) rather
# than being panels themselves, so they carry no meta.panel annotation.
# ---------------------------------------------------------------------------

def _comention_graph():
    """Build (node_ids, edges, names) from the live KG store."""
    store = _get_kg_store()
    nodes = list(store._nodes.keys())
    names = {nid: getattr(node, "name", nid) for nid, node in store._nodes.items()}
    edges = [
        (t.subject, t.object)
        for t in store._triples.values()
        if t.subject and t.object and t.subject != t.object
    ]
    return nodes, edges, names


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "kg": {"type": ["string", "null"]},
            "community_count": {"type": "integer"},
            "communities": {"type": "array"},
            "assignments": {"type": "array"},
        }
    ),
    # Data-mode (M1.3): the community-coloring payload for the entity_graph
    # panel, servable through the /api/v1/ui/data proxy. Base nodes/edges stay
    # available via the existing /api/v1/entity_graph REST route.
    meta={"data": {"panel": "entity_graph", "rest_route": "/api/v1/entity_graph"}},
)
def kg_communities(kg: Optional[str] = None) -> dict:
    """Community detection (label propagation) over the KG co-mention graph,
    for colouring the entity graph. Accepts an optional ``kg`` namespace
    (Track P) — the default graph is used until namespacing lands.
    """
    try:
        from src.analytics.kg_analytics import kg_communities_payload

        nodes, edges, names = _comention_graph()
        return kg_communities_payload(nodes, edges, names, kg=kg)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "kg": {"type": ["string", "null"]},
            "edges": {"type": "integer"},
            "nodes_ranked": {"type": "array"},
        }
    ),
)
def kg_centrality(kg: Optional[str] = None, top: int = 20) -> dict:
    """PageRank centrality over the KG co-mention graph, for sizing entity-graph
    nodes. Accepts an optional ``kg`` namespace (Track P).

    Args:
        kg:  optional KG namespace label.
        top: number of top-centrality nodes to return (default 20).
    """
    try:
        from src.analytics.kg_analytics import kg_centrality_payload

        nodes, edges, names = _comention_graph()
        return kg_centrality_payload(nodes, edges, names, kg=kg, top=top)
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
