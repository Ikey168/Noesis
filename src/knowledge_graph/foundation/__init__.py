"""
Knowledge graph foundation: typed ontology, reified provenance-bearing triples,
and a backend-agnostic store.

Replaces the implicit, untyped entity-relationship graph with a real knowledge
graph centered on concepts and claims, where documents are anchors and every
fact is a cited triple. See ``docs/architecture/knowledge-engine-pivot.md``.
"""

from src.knowledge_graph.foundation.model import (
    Node,
    Provenance,
    Triple,
    make_node_id,
)
from src.knowledge_graph.foundation.ontology import (
    EntityType,
    OntologyViolation,
    RelationType,
    ancestors,
    allowed_pairs,
    is_subtype,
    is_valid_relation,
    validate_relation,
)
from src.knowledge_graph.foundation.resolution import (
    EntityResolver,
    canonicalize_store,
)
from src.knowledge_graph.foundation.store import KnowledgeGraphStore

__all__ = [
    "EntityResolver",
    "canonicalize_store",
    "EntityType",
    "RelationType",
    "OntologyViolation",
    "ancestors",
    "allowed_pairs",
    "is_subtype",
    "is_valid_relation",
    "validate_relation",
    "Node",
    "Provenance",
    "Triple",
    "make_node_id",
    "KnowledgeGraphStore",
]
