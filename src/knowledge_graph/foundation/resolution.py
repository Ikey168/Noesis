"""
Entity resolution: collapse different surface forms of the same real-world
entity into one canonical knowledge-graph node.

Without this, "Geoffrey Hinton", "Hinton", and "G. Hinton" become three nodes
and the graph fragments. The resolver assigns canonical entity ids that survive
across documents, using (in order) an alias index, name-aware matching for
people, generic fuzzy/containment matching, and an optional embedding
similarity fallback. It can also backfill an existing store, merging duplicate
nodes and rewriting triples to canonical ids.

See ``docs/architecture/knowledge-engine-pivot.md``.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.knowledge_graph.foundation.model import Node, Triple, make_node_id
from src.knowledge_graph.foundation.ontology import EntityType
from src.knowledge_graph.foundation.store import KnowledgeGraphStore

Embedder = Callable[[str], Sequence[float]]

_ORG_SUFFIX = re.compile(
    r"\b(inc|llc|corp|corporation|ltd|co|company|plc|gmbh|sa|ag)\b", re.IGNORECASE
)


def _normalize_name(entity_type: EntityType, name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace; drop org suffixes."""
    text = (name or "").lower()
    if entity_type == EntityType.ORGANIZATION:
        text = _ORG_SUFFIX.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)  # punctuation -> space (so "g." -> "g")
    return re.sub(r"\s+", " ", text).strip()


def _token_compatible(x: str, y: str) -> bool:
    """Two name tokens match if equal or one is an initial of the other."""
    if x == y:
        return True
    if len(x) == 1 and y.startswith(x):
        return True
    if len(y) == 1 and x.startswith(y):
        return True
    return False


def _person_compatible(a_norm: str, b_norm: str) -> bool:
    """Name-aware match for people: same surname, compatible given names.

    Handles surname-only ("Hinton"), initials ("G. Hinton"), and full names
    ("Geoffrey Hinton") resolving together, while keeping different surnames
    ("John Smith" vs "Jane Smith") apart.
    """
    ta, tb = a_norm.split(), b_norm.split()
    if not ta or not tb:
        return False
    if ta[-1] != tb[-1]:  # surnames must match
        return False
    given_a, given_b = ta[:-1], tb[:-1]
    shorter, longer = (given_a, given_b) if len(given_a) <= len(given_b) else (given_b, given_a)
    used = [False] * len(longer)
    for tok in shorter:
        for i, other in enumerate(longer):
            if not used[i] and _token_compatible(tok, other):
                used[i] = True
                break
        else:
            return False
    return True


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0 or nv == 0:
        return 0.0
    return dot / (nu * nv)


class EntityResolver:
    """Assigns canonical entity ids, merging surface variants of one entity."""

    def __init__(
        self,
        similarity_threshold: float = 0.88,
        embedder: Optional[Embedder] = None,
        embedding_threshold: float = 0.83,
    ):
        self.similarity_threshold = similarity_threshold
        self.embedder = embedder
        self.embedding_threshold = embedding_threshold
        # canonical node_id -> Node
        self._canonical: Dict[str, Node] = {}
        # (type, normalized surface form) -> canonical node_id
        self._exact: Dict[Tuple[EntityType, str], str] = {}
        # type -> list of canonical node_ids (fuzzy scan space)
        self._by_type: Dict[EntityType, List[str]] = {}
        self._embedding_cache: Dict[str, Sequence[float]] = {}

    # ---- public API ----------------------------------------------------- #

    def resolve(
        self,
        entity_type: EntityType,
        name: str,
        aliases: Optional[Sequence[str]] = None,
        properties: Optional[dict] = None,
    ) -> Node:
        """Return the canonical Node for ``name``, creating it if new.

        The surface form (and any provided aliases) are registered on the
        canonical node so future variants resolve to the same id.
        """
        if not isinstance(entity_type, EntityType):
            entity_type = EntityType(entity_type)
        norm = _normalize_name(entity_type, name)
        surfaces = [name] + list(aliases or [])

        match = self._find_match(entity_type, name, norm)
        if match is not None:
            self._register(match, entity_type, surfaces)
            # Prefer the most complete surface form as the display name.
            if len(name.split()) > len(match.name.split()):
                match.name = name
            if properties:
                match.properties.update(properties)
            return match

        canonical = Node(
            type=entity_type,
            name=name,
            aliases=list(dict.fromkeys(surfaces)),
            properties=dict(properties or {}),
        )
        self._canonical[canonical.node_id] = canonical
        self._by_type.setdefault(entity_type, []).append(canonical.node_id)
        self._register(canonical, entity_type, surfaces)
        return canonical

    @property
    def canonical_nodes(self) -> List[Node]:
        return list(self._canonical.values())

    def canonical_count(self, entity_type: Optional[EntityType] = None) -> int:
        if entity_type is None:
            return len(self._canonical)
        return len(self._by_type.get(entity_type, []))

    # ---- matching ------------------------------------------------------- #

    def _find_match(self, entity_type: EntityType, name: str, norm: str) -> Optional[Node]:
        if not norm:
            return None
        exact_id = self._exact.get((entity_type, norm))
        if exact_id is not None:
            return self._canonical[exact_id]

        best: Optional[Node] = None
        best_score = 0.0
        for node_id in self._by_type.get(entity_type, []):
            cand = self._canonical[node_id]
            cand_forms = [cand.name] + cand.aliases
            cand_norms = {_normalize_name(entity_type, f) for f in cand_forms}

            if entity_type in (EntityType.PERSON,):
                if any(_person_compatible(norm, cn) for cn in cand_norms):
                    return cand
                continue

            for cn in cand_norms:
                if not cn:
                    continue
                if self._token_containment(norm, cn):
                    return cand
                score = _ratio(norm, cn)
                if score > best_score:
                    best, best_score = cand, score

        if best is not None and best_score >= self.similarity_threshold:
            return best

        if self.embedder is not None:
            return self._embedding_match(entity_type, name)
        return None

    @staticmethod
    def _token_containment(a: str, b: str) -> bool:
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return False
        return ta <= tb or tb <= ta

    def _embedding_match(self, entity_type: EntityType, name: str) -> Optional[Node]:
        vec = self._embed(name)
        best: Optional[Node] = None
        best_sim = 0.0
        for node_id in self._by_type.get(entity_type, []):
            cand = self._canonical[node_id]
            sim = _cosine(vec, self._embed(cand.name))
            if sim > best_sim:
                best, best_sim = cand, sim
        if best is not None and best_sim >= self.embedding_threshold:
            return best
        return None

    def _embed(self, name: str) -> Sequence[float]:
        if name not in self._embedding_cache:
            self._embedding_cache[name] = self.embedder(name)
        return self._embedding_cache[name]

    def _register(self, canonical: Node, entity_type: EntityType, surfaces: Sequence[str]) -> None:
        for surface in surfaces:
            if surface and surface not in canonical.aliases:
                canonical.aliases.append(surface)
            self._exact[(entity_type, _normalize_name(entity_type, surface))] = canonical.node_id


def canonicalize_store(
    store: KnowledgeGraphStore,
    resolver: Optional[EntityResolver] = None,
    embedder: Optional[Embedder] = None,
) -> Tuple[KnowledgeGraphStore, Dict[str, str]]:
    """Backfill an existing store: merge duplicate nodes, remap triples.

    Returns a new canonicalized store plus an ``old_id -> canonical_id`` map.
    Re-asserted facts merge (accumulating provenance); triples that collapse to
    a self-loop after merging are dropped.
    """
    resolver = resolver or EntityResolver(embedder=embedder)
    new_store = KnowledgeGraphStore()
    id_map: Dict[str, str] = {}

    # Resolve every existing node to a canonical node.
    for entity_type in EntityType:
        for node in store.nodes_by_type(entity_type):
            canonical = resolver.resolve(node.type, node.name, node.aliases, node.properties)
            id_map[node.node_id] = canonical.node_id

    for canonical in resolver.canonical_nodes:
        new_store.add_node(canonical)

    # Remap and re-add triples with their original provenance/properties.
    for triple in store.triples():
        subj = id_map.get(triple.subject, triple.subject)
        obj = id_map.get(triple.object, triple.object)
        if subj == obj:
            continue
        for prov in store.provenance_for(triple):
            new_store.add_triple(
                Triple(subj, triple.predicate, obj, provenance=prov, properties=dict(triple.properties))
            )

    return new_store, id_map
