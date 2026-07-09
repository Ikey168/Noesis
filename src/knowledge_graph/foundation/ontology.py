"""
Knowledge graph ontology: typed entities, typed relations, and constraints.

This replaces the implicit, untyped entity-relationship graph (where nodes were
news ``Article`` vertices and edges were inferred by co-occurrence) with a
typed ontology that validates what entity and relation types may exist and which
relations are allowed between which entity types.

See ``docs/architecture/knowledge-engine-pivot.md`` (knowledge graph
foundation).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Set, Tuple


class EntityType(str, Enum):
    """Typed node classes in the knowledge graph.

    ``ENTITY`` is the root; every other type is a subtype of it. ``DOCUMENT`` is
    an anchor type (a source a fact came from); ``CONCEPT`` and ``CLAIM`` are the
    knowledge-bearing types the graph is centered on.
    """

    ENTITY = "Entity"
    PERSON = "Person"
    ORGANIZATION = "Organization"
    CONCEPT = "Concept"
    DOCUMENT = "Document"
    CLAIM = "Claim"
    METHOD = "Method"
    DATASET = "Dataset"


# is-a hierarchy: child -> direct parent. ENTITY is the root.
# METHOD is a kind of CONCEPT; everything else hangs directly off ENTITY.
_PARENT: Dict[EntityType, EntityType] = {
    EntityType.PERSON: EntityType.ENTITY,
    EntityType.ORGANIZATION: EntityType.ENTITY,
    EntityType.CONCEPT: EntityType.ENTITY,
    EntityType.DOCUMENT: EntityType.ENTITY,
    EntityType.CLAIM: EntityType.ENTITY,
    EntityType.METHOD: EntityType.CONCEPT,
    EntityType.DATASET: EntityType.ENTITY,
}


def ancestors(entity_type: EntityType) -> Set[EntityType]:
    """Return ``entity_type`` plus all of its supertypes (including ENTITY)."""
    chain: Set[EntityType] = {entity_type}
    current = entity_type
    while current in _PARENT:
        current = _PARENT[current]
        chain.add(current)
    return chain


def is_subtype(child: EntityType, parent: EntityType) -> bool:
    """True if ``child`` is ``parent`` or a (transitive) subtype of it."""
    return parent in ancestors(child)


class RelationType(str, Enum):
    """Typed, directed edges in the knowledge graph."""

    AUTHORED_BY = "AUTHORED_BY"
    CITES = "CITES"
    INSTANCE_OF = "INSTANCE_OF"
    PART_OF = "PART_OF"
    DEFINES = "DEFINES"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    MENTIONS = "MENTIONS"


# Allowed (subject_type, object_type) pairs per relation. Matching is
# subtype-aware: a subject/object satisfies a pair if it is-a the listed type.
_CONSTRAINTS: Dict[RelationType, FrozenSet[Tuple[EntityType, EntityType]]] = {
    RelationType.AUTHORED_BY: frozenset({
        (EntityType.DOCUMENT, EntityType.PERSON),
        (EntityType.DOCUMENT, EntityType.ORGANIZATION),
    }),
    RelationType.CITES: frozenset({
        (EntityType.DOCUMENT, EntityType.DOCUMENT),
    }),
    RelationType.INSTANCE_OF: frozenset({
        (EntityType.ENTITY, EntityType.CONCEPT),
    }),
    RelationType.PART_OF: frozenset({
        (EntityType.CONCEPT, EntityType.CONCEPT),
        (EntityType.DOCUMENT, EntityType.DOCUMENT),
    }),
    RelationType.DEFINES: frozenset({
        (EntityType.DOCUMENT, EntityType.CONCEPT),
    }),
    RelationType.SUPPORTS: frozenset({
        (EntityType.DOCUMENT, EntityType.CLAIM),
        (EntityType.CLAIM, EntityType.CLAIM),
    }),
    RelationType.CONTRADICTS: frozenset({
        (EntityType.DOCUMENT, EntityType.CLAIM),
        (EntityType.CLAIM, EntityType.CLAIM),
    }),
    RelationType.MENTIONS: frozenset({
        (EntityType.DOCUMENT, EntityType.ENTITY),
    }),
}


class OntologyViolation(ValueError):
    """Raised when a node or relation violates the ontology."""


def allowed_pairs(relation: RelationType) -> FrozenSet[Tuple[EntityType, EntityType]]:
    return _CONSTRAINTS[relation]


def is_valid_relation(
    relation: RelationType,
    subject_type: EntityType,
    object_type: EntityType,
) -> bool:
    """True if ``subject_type --relation--> object_type`` is permitted.

    A triple is valid when the subject is-a the allowed domain type and the
    object is-a the allowed range type for at least one configured pair.
    """
    for domain, range_ in _CONSTRAINTS[relation]:
        if is_subtype(subject_type, domain) and is_subtype(object_type, range_):
            return True
    return False


def validate_relation(
    relation: RelationType,
    subject_type: EntityType,
    object_type: EntityType,
) -> None:
    """Raise :class:`OntologyViolation` if the relation is not permitted."""
    if not is_valid_relation(relation, subject_type, object_type):
        permitted = ", ".join(
            f"{d.value}->{r.value}" for d, r in sorted(_CONSTRAINTS[relation])
        )
        raise OntologyViolation(
            f"{relation.value} not allowed from {subject_type.value} to "
            f"{object_type.value}; permitted: {permitted}"
        )
