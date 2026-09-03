# Knowledge graph foundation

Turns the implicit, untyped entity-relationship graph (nodes were news `Article`
vertices, edges were inferred by co-occurrence) into a real knowledge graph:
typed entities, typed relations, reified provenance-bearing triples, and a
backend-agnostic store that enforces an ontology on every write.

Part of the knowledge-engine pivot; see
`docs/architecture/knowledge-engine-pivot.md`.

## What changed conceptually

| Old (ER graph) | New (knowledge graph) |
| --- | --- |
| Nodes are documents (`Article`) | Nodes are typed; documents are anchors, `Concept`/`Claim` are the knowledge |
| Edges inferred by co-occurrence | Typed relations validated against an ontology |
| No provenance | Every triple carries `(source_doc, chunk_id, confidence, extractor)` |
| Flat edges | Reified edges carry properties (e.g. `CITES.year`) |

## Modules

- `ontology.py`: `EntityType` (with an is-a hierarchy rooted at `Entity`),
  `RelationType`, and subtype-aware domain/range constraints
  (`is_valid_relation`, `validate_relation`).
- `model.py`: `Node`, `Provenance` (required on every fact), and `Triple`
  (reified, provenance-bearing edge with arbitrary properties).
- `store.py`: `KnowledgeGraphStore` for isolated tests and
  `DuckDBKnowledgeGraphStore` for durable production use. Both enforce the
  ontology, require endpoints to exist, and accumulate provenance when a fact
  is re-asserted.
- `resolution.py`: `EntityResolver` assigns canonical entity ids so different
  surface forms of one entity collapse into a single node, and
  `canonicalize_store` backfills an existing store (merging duplicate nodes and
  rewriting triples to canonical ids).

## Entity resolution

`EntityResolver` matches a candidate against existing canonical entities of the
same type, in order:

1. **Alias index** exact match on the normalized surface form.
2. **Name-aware matching** for people: same surname with compatible given names,
   so "Hinton", "Geoffrey Hinton", and "G. Hinton" resolve together while
   "John Smith" and "Jane Smith" stay apart.
3. **Generic fuzzy/containment** matching (token containment or
   `SequenceMatcher` ratio above a threshold), handling organization suffixes
   ("OpenAI" / "OpenAI Inc." / "Open AI") and plurals ("Transformer" /
   "Transformers").
4. **Embedding similarity** fallback when an `embedder` is supplied, catching
   lexically distant aliases ("NYC" / "New York City").

```python
from src.knowledge_graph.foundation import EntityResolver, EntityType

r = EntityResolver()
r.resolve(EntityType.PERSON, "Hinton")
r.resolve(EntityType.PERSON, "Geoffrey Hinton")  # same canonical node
```

## Ontology

Entity types: `Entity` (root), `Person`, `Organization`, `Concept`, `Document`,
`Claim`, `Method` (a kind of `Concept`), `Dataset`.

Relation types and where they are allowed:

| Relation | Permitted (subject -> object) |
| --- | --- |
| `AUTHORED_BY` | Document -> Person, Document -> Organization |
| `CITES` | Document -> Document |
| `INSTANCE_OF` | Entity -> Concept |
| `PART_OF` | Concept -> Concept, Document -> Document |
| `DEFINES` | Document -> Concept |
| `SUPPORTS` | Document -> Claim, Claim -> Claim |
| `CONTRADICTS` | Document -> Claim, Claim -> Claim |
| `MENTIONS` | Document -> Entity (any subtype) |

Matching is subtype-aware: where a relation permits `Entity`, any subtype
(`Person`, `Concept`, ...) satisfies it.

## Usage

```python
from src.knowledge_graph.foundation import (
    KnowledgeGraphStore, Node, Triple, Provenance, EntityType, RelationType,
)

kg = KnowledgeGraphStore()
paper = kg.add_node(Node(EntityType.DOCUMENT, "Attention Is All You Need"))
concept = kg.add_node(Node(EntityType.CONCEPT, "Transformer"))

kg.add_triple(Triple(
    paper.node_id, RelationType.DEFINES, concept.node_id,
    provenance=Provenance(source_doc=paper.node_id, confidence=0.9, chunk_id="abstract"),
))
```

## Persistence boundary

The API/ingestion writer owns the read-write DuckDB connection. Standalone MCP
servers open the same file read-only, and correction approval synchronizes the
durable store. The in-memory class remains only for fast, isolated tests.
