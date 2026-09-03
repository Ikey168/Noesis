# Knowledge graph

Noesis has one local-first graph architecture. The typed foundation in
`foundation/` stores ontology-validated nodes and provenance-bearing triples;
`DuckDBKnowledgeGraphStore` persists those records in the same configured
warehouse used by the corpus. `kg_updater.py` is the ingestion projection, and
`tools/kg_mcp/server.py` opens the persisted graph read-only when deployed as a
standalone MCP process.

## Data model

- `kg_nodes`: typed entities (`Document`, `Person`, `Organization`, `Concept`,
  `Claim`, `Method`, and `Dataset`);
- `kg_triples`: typed relations validated against the ontology;
- `kg_provenance`: every assertion's source document, optional chunk,
  extractor, and confidence;
- `kg_mutation_events`: the durable feed for evolving-topic and emerging-edge
  views;
- `canonical_entities`, `entity_aliases`, and `document_relations`: the shared
  canonical-entity projection used by the KB contract.

Approved corrections update the durable graph and survive process restarts.
Repeated assertions accumulate provenance rather than duplicating facts.

## Usage

```python
from src.knowledge_graph.foundation import DuckDBKnowledgeGraphStore

store = DuckDBKnowledgeGraphStore("data/local_warehouse.duckdb")
print(store.node_count, store.triple_count)
store.close()
```

For read-only agent access:

```bash
NOESIS_DB_PATH=data/local_warehouse.duckdb python tools/kg_mcp/server.py
```

The legacy AWS graph implementation was removed. Graph analytics such as
community detection and centrality use `src/analytics/graph.py` over the
persisted local graph, so no external graph database or cloud endpoint is
required.

## Tests

```bash
pytest tests/knowledge_graph/test_kg_updater.py \
  tests/knowledge_graph/test_entity_corrections.py \
  tests/knowledge_graph/test_kg_persistence.py
```
