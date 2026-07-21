# Knowledge domains: one abstraction, two backings

Noesis synthesizes the ingested corpus into **knowledge domains** (news,
economics, technology, web3, papers, …) that applications consume through the
KB contract. The domain layer lives in `src/kb/` and is deliberately distinct
from `src/domains/` (domain *packs*, which extend behaviour — enrichers,
routes, flags). A KB domain scopes *content*.

## The abstraction

`config/domains.yml` is the single source of truth. The
`KnowledgeDomainRegistry` (`src/kb/registry.py`) validates it and resolves
each domain name to a `DomainBacking` (`src/kb/backing.py`) — the read
interface every domain answers:

| Surface | Calls |
|---|---|
| retrieve | `documents`, `search`, `claims`, `entities` |
| diff | `diff(since)` |
| meta | `coverage()` |

Consumers only ever hold `DomainBacking`. They must never be able to tell
which backing serves them; `coverage()` *reports* the backing as metadata, but
answer shapes are identical across backings.

## The two backings

- **`corpus-view`** — membership rows (`document_domains`) + views over the
  shared `documents` sink. Right for overlapping daily topics: heavy
  cross-domain overlap, shared enrichments, cheap joins, one consolidation
  pass covers them all. A document can belong to several domains — that is
  the point of views.
- **`namespace`** — a provisioned knowledge graph (the provisioning plane)
  with its own storage, pipelines, quotas, and teardown. Right for domains
  with their own lifecycle: a reference corpus of papers/books with different
  retention and re-index cadence than the daily stream, experimental domains,
  agent-provisioned domains.

### Default policy: start as a view, promote when a domain earns isolation

Promotion (`corpus-view` → `namespace`, or the reverse) is a registry pointer
flip plus a data migration through the provisioning plane's ingest tools,
preserving document ids so links survive. Because consumers only saw the
contract, nothing downstream changes.

## The shared embedding space

Cross-domain and cross-backing similarity (claim linking, depth linkage
between daily domains and the reference corpus) only means anything inside a
single embedding space. Therefore every domain **must** declare
`embedding_model` in its definition; the registry exposes
`embedding_models()` for consistency checks, and `coverage()` surfaces each
domain's model so a stale backing fails loudly instead of returning silently
bad similarity.

## Roadmap anchors

- Corpus-view data paths (membership pass, per-domain views): #962
- Starter domains + curated feeds: #963
- Namespace backing + cross-backing linkage: #967
- KB contract v1 over both backings: #970
