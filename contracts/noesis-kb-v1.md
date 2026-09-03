# noesis-kb-v1 — the knowledge-base query contract

The versioned surface applications build against. Served identically over
MCP (`tools/kb_mcp/server.py`) and REST (`/api/v1/kb/...`); both are thin
adapters over one implementation (`src/kb/contract.py`), which routes every
answer through the domain registry to a `DomainBacking`. **A consumer must
never be able to tell whether a domain is corpus-view or namespace backed** —
`coverage` reports the backing as metadata, but answer shapes are identical.
That guarantee is enforced by `tests/unit/kb/test_contract.py`, which runs
the same suite against both backings and diffs the shapes.

## Envelope

Every successful response:

```json
{
  "contract": "noesis-kb-v1",
  "domain": "web3",
  "as_of_ms": 1784700000000,
  "data": "…call-specific…"
}
```

Errors carry a stable code — MCP: `{"error": {"code", "message"}}`;
REST: HTTP 404 (`unknown_domain`) / 400 (`bad_request`, `bad_since`) with the
same object as `detail`.

`since` parameters are ISO-8601, UTC assumed when naive, offsets honoured.
They filter on **ingestion time**: a backfilled 2020 paper ingested today is
new domain content today.

## Surface

| Call | Data shape |
|---|---|
| `kb_domains()` | `[{name, backing, description, embedding_model}]` |
| `kb_search(domain, query, limit)` | document rows (cited: id, title, url, source, domain score/method) — lexical; wildcards are literals |
| `kb_documents(domain, since?, limit)` | document rows, newest arrival first |
| `kb_claims(domain, since?, limit)` | **clusters**: `{cluster_id, representative, citations[], corroboration, contradictions[], size}` — representative = recency + source quality, never a superseded member while a live one exists; every citation carries source/url; contradictions carry `prediction_mode` + confidence |
| `kb_entities(domain, name?)` | `[{canonical_id, name, mentions, aliases[]}]`, alias mentions folded |
| `kb_contradictions(domain, since?)` | contradiction ledger entries, both sides cited, `prediction_mode` + confidence |
| `kb_diff(domain, since)` | six sections: `documents {new, total, sources_delivered}`, `new_clusters`, `gained_corroboration` (new sources named), `new_contradictions`, `superseded`, `entity_surges` (`null` where a backing has no mention timeline — an honest gap, never a silent `[]`), plus `meta {as_of_ms, since_ms, consolidation watermarks}` |
| `kb_integrity(domain, document_id?, limit?)` | honesty-enveloped per-document snapshots, revisions, media provenance/C2PA, image reuse and cross-modal findings; every finding has evidence locators and a silent edit cites both versions |
| `kb_coverage(domain)` | corpus stats, freshness, sources, backing, `embedding_model`, mismatch counts — so a consumer can honestly say when coverage is thin |

## Guarantees

- **Citations always.** Any claim/contradiction entry names its documents and
  sources; uncited content is flagged, never silent.
- **Honest uncertainty.** Model-derived entries carry `prediction_mode`
  (`heuristic` | `zero-shot:<model>` | …) and calibrated-ish confidence, per
  the platform honesty contract.
- **As-of everywhere.** Every envelope timestamps itself; `kb_diff.meta`
  additionally names the consolidation run watermarks the answer was
  computed against (passes commit transactionally — a diff never observes a
  half-finished run).
- **Stability.** Internal schema changes without a contract bump must keep
  the shape tests green. Breaking changes mean `noesis-kb-v2`, not a
  mutation of this document.
