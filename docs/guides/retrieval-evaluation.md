# Retrieval quality and partial results

`services.rag.retriever.HybridRetriever.search_detailed` returns results alongside per-source outcomes, candidate limits, elapsed time and unmetered-cost disclosure. Candidate defaults scale to twice the requested result count, bounded at 1,000. Requests above 20 are supported. A configured provider failure or missing vector input produces `partial`, while a successful empty provider response is `complete`. With no providers the outcome is `unavailable`. An enabled reranker must execute its actual model; fallback scoring is not reported as cross-encoder success. The compatibility `search`/`hybrid_search` list path raises `PartialRetrievalError` with the detailed response when incomplete.

Consumer mapping from the implementation review:

| Entry point | Actual retrieval path |
|---|---|
| Python `HybridRetriever` and `hybrid_search` | PostgreSQL lexical/vector adapters with optional cross-encoder; no production API/MCP importer was found |
| Knowledge-engine MCP `query_knowledge` | `src.kb.unified_query`, scoped warehouse/adapter query with its own coverage/deadline contract |
| `/ask` API | `services.rag.answer.RAGAnswerService`, a separate implementation; its 20-result request limit does not configure `HybridRetriever` |

Do not infer that a benchmark of one path validates the others. The changes here do not redirect `/ask` or the knowledge-engine query engine.

## Frozen evaluation

Install the `evaluation` extra and run `python scripts/evaluate_retrieval.py manifest.json --out report.json`. The scorer uses [ir-measures](https://ir-measur.es/en/latest/getting-started.html) for Recall, nDCG, reciprocal rank and judged fraction at each cutoff, including a required cutoff above 20. It reports per-query/domain metrics, observed latency percentiles, explicitly supplied costs and incomplete query IDs. Empty/failed queries remain in the denominator. Unknown costs remain null.

The manifest contract is `noesis-retrieval-eval-v1`:

- `label_origin`: `human`; `reviewers`: at least two distinct IDs; `adjudication_record`: the independent review record reference.
- `queries`: bounded objects with `id`, `domain`, `text`; `cutoffs`: for example `[10, 50]`.
- `qrels`: `{ "path": "qrels.json", "sha256": "<frozen file SHA-256>" }`; its file maps query IDs to document IDs and integer relevance grades 0–3.
- `runs`: exactly `lexical`, `semantic`, `fusion`, `reranked`, each with `path`, `sha256`, and `configuration` identifying the actual consumer, provider/model revision and retrieval parameters. Each run file maps every query ID to `{ "results": [{ "id": "document-id", "score": 0.8 }], "latency_ms": 12, "usd_micros": null, "status": "complete" }`.

Member hashes freeze judgments and results; changed members fail. Run files must come from the actual selected path. The scorer does not execute providers or fabricate runs. A review record and identities are supplied provenance, not independently authenticated human participation. Production readiness requires auditing that provenance and the judgment protocol, including source/domain coverage. Judged fraction discloses incomplete relevance pools; measured recall is relative to those pools.

`--allow-fixture` permits explicitly marked behavior fixtures for testing the scorer. Those reports retain `label_origin: fixture`. The regression suite checks known metric values and missing outcomes; no independent human retrieval benchmark has yet been supplied, so QA-02 quality validation remains pending.
