# Legacy surfaces

This tree contains retired NeuroNews/early-Noesis application surfaces kept for
historical reproducibility and regression coverage. They are **not supported
product entry points** and must not be used as dependencies by current Noesis
code.

Moved here:

- `legacy/src/main.py` — original NeuroNews scraper wrapper. Use
  `python -m src.scraper.run` instead.
- `legacy/src/scraper.py` — shadowed backward-compatibility scraper facade;
  current code lives in the `src.scraper` package.
- `legacy/services/api/` — standalone Issue #233-era FastAPI `/ask` service.
  Use `src.api.app` / `noesis serve --surface api` instead.
- `legacy/services/rag/answer.py` — simulated RAG answer service with sample
  vector-search results and canned generation. Use `src.kb.contract.kb_answer`,
  context assembly, and the supported KB/API/MCP surfaces instead.
- `legacy/evals/`, `legacy/validation/evals/`, and legacy demo scripts —
  evaluation/demo code coupled to the retired `/ask` service.

Reusable RAG utilities such as chunking, normalization, lexical/vector
retrieval, reranking, and their evaluation harnesses remain outside this tree
because they still have active consumers.

Legacy code may import current libraries for reproducibility, but current
production code must never import `legacy.*`.
