# Provisioned-domain acceptance report (R9)

Track P (the provisioning plane, R8) claims that a new knowledge domain can be
stood up on the canvas with no pack code and no deploy: an agent deploys a
namespaced knowledge graph, binds the sources that feed it, routes matching
documents in, and the generative UI grows a scoped panel for it via discovery.
R9 is the acceptance of that claim. It provisions **two** domains this way, to
show the R8 result was not a fluke.

The executable form of this report is
[`scripts/provisioning/acceptance.py`](../scripts/provisioning/acceptance.py),
which drives the real `provisioning_mcp` server over a FastMCP client on a
throwaway warehouse. The committed regression is
[`tests/unit/provisioning/test_acceptance.py`](../tests/unit/provisioning/test_acceptance.py).

## The two domains

| Domain | Corpus | Sources | Attach method |
|---|---|---|---|
| `finance` | earnings-call transcripts | Acme Earnings, Globex Calls | explicit source list |
| `legal` | policy and rule filings | Federal Register | criteria: `min_transparency >= 0.85, type=news` |

Both share one `news_articles` corpus that also carries an unrelated news
outlet (City News). Routing must keep each namespace to its own sources and
never leak the news rows into either.

## Steps taken (per domain)

Each domain goes through the same provisioning-only sequence, no code written:

1. `kg_deploy(name, description)` returns a free dry-run preview (nothing is
   written).
2. `kg_deploy(name, description, approve=True)` creates the `kg_<name>_*`
   namespace and registers the deploy in lineage.
3. `kg_attach_sources(kg, sources=[...])` or `kg_attach_sources(kg,
   criteria={...})` binds the feeds. The criteria path resolves against the
   outlet transparency scores, so `legal` selects Federal Register (0.90) and
   rejects the lower-scored finance outlets.
4. `kg_ingest(kg)` routes the bound sources' documents (and their claims and
   derived entities) into the namespace tables.
5. `kg_status(kg)` / `kg_view(kg)` / `kg_lineage(kg)` read the result: counts,
   the scoped documents/entities/claims family, and the event log.

## Result (live run)

```
Standing up two domains by provisioning alone (no pack code):
  [finance] deploy -> attach (2 sources) -> ingest (3 docs, 2 claims, 3 entities) in 272 ms
  [legal]   deploy -> attach (1 sources) -> ingest (2 docs, 1 claims, 1 entities) in 252 ms
kg_list: 2 domains live -> ['finance', 'legal']
  [finance] scoped family: 3 docs, 3 entities, 2 claims; lineage ['attach', 'deploy', 'ingest']
  [legal]   scoped family: 2 docs, 1 entities, 1 claims; lineage ['attach', 'deploy', 'ingest']
shared news_articles still 6 rows (untouched)
RESULT: OK - two domains live via provisioning alone (R9 exit criterion)
```

- **Time-to-live**: roughly 250-270 ms per domain, deploy to ingested, on the
  in-process FastMCP transport.
- **Isolation**: `kg_finance_documents` holds exactly `{f1, f2, f3}` and
  `kg_legal_documents` holds exactly `{l1, l2}`; the news row `n1` reached
  neither, and the two namespaces are disjoint.
- **Scoped family**: each namespace exposes its own documents, top entities and
  claims through `kg_view(kg)`, surfaced on the canvas by the discovered
  `provisioned_kg` panel scoped by the `kg` parameter.
- **Shared corpus**: `news_articles` and `argument_claims` are read, never
  written; both retain their original row counts.

## Guardrails exercised

- **Approval gate**: every deploy was previewed before it was approved; the
  preview wrote nothing.
- **Criteria resolution**: `legal` was bound by a quality criterion, not an
  explicit list, proving source selection can be quality-driven.
- **Idempotency and provenance**: re-running is covered by the R8 convergence
  tests; every deploy/attach/ingest appears in the lineage log read back here.

## Friction found (feeding back into R8)

1. **Scoped panel family was counts-only.** After R8, `kg_view` returned only
   per-KG counts, not the documents/entities/claims samples that a scoped
   `documents` / `entity_graph` / `claims` family needs. Fixed in this change:
   `namespaces.namespace_sample` plus `Provisioner.view` return the scoped
   sample, and the `provisioned_kg` panel renders the three sub-sections. No
   pack code; the fix lives in the provisioning plane.
2. **Corpus is `news_articles`-shaped.** Routing reads the `news_articles`
   table and labels routed documents `source_type='news'`. That is fine for
   any corpus a connector writes there (earnings transcripts and filings
   included, keyed by `source`), but a future domain landing in a different
   table would need routing to widen its source corpus. Logged as an R8
   follow-up; it did not block either domain here.

Neither item required a domain pack. The two domains are live as data plus
provisioning state alone, which is the R9 exit criterion.
