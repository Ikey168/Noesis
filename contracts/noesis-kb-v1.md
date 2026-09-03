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
| `kb_answer(domain, question, limit, minimum_relevance)` | additive `noesis-answer-v1` payload with statement-level verdicts, separate supporting/contradicting evidence, explicit refusal, and a reproducible evidence plan |
| `kb_search_domains(query, domains? \| all_authorized, limits?, principal?)` | additive `noesis-cross-domain-v1` scope receipt plus a deduplicated reciprocal-rank-fusion result set; every hit retains all domain/backing retrieval receipts |
| `kb_answer_domains(question, domains? \| all_authorized, limits?, principal?)` | one `noesis-answer-v1` synthesis over the selected domains; statement evidence names its domains/backings and the plan reports per-domain coverage and failures |
| `kb_cross_links(domains? \| all_authorized, kind?, relation?, limit?)` | inspectable entity equivalences and claim links across domains, including confidence, method, model/run provenance, endpoint evidence, and reversibility |
| `kb_temporal(domain, assertion_kind?, assertion_id?, as_of?, valid_at?, observed_before?, history?, include_retracted?, limit?, cursor?)` | additive `noesis-temporal-v1` snapshot/history query over independent valid and observation clocks; returns immutable documents, claims, entities, relations, or observations with precision, provenance, typed revision transitions, coverage limitations, and stable pagination |
| `kb_corroborate(domain, claim_id)` | origin-aware publication, probable-origin, unresolved, and dependency-evidence counts; distinct-source compatibility fallback |
| `watch_create/list/poll/pause/resume/delete(...)` | additive `noesis-claim-watch-v1` lifecycle and opaque-cursor event polling, principal/domain scoped |
| `policy_monitor_status(principal_id?, include_private?)` | additive `noesis-policy-monitor-v1` cited receipt; public by default, grant-gated when private is explicit |
| `policy_monitor_bundle(principal_id?, include_private?)` | offline-verifiable bundle of that receipt with the same private-evidence gate |
| `kb_documents(domain, since?, limit)` | document rows, newest arrival first |
| `kb_claims(domain, since?, limit)` | **clusters**: `{cluster_id, representative, citations[], corroboration, independence, contradictions[], size}` — `corroboration` remains the compatible integer while `independence` separates publications, probable origins, and unresolved lineage; representative = recency + source quality, never a superseded member while a live one exists |
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

## Cross-domain scope

Cross-domain operations are an additive extension; every single-domain call
above remains unchanged. A caller supplies exactly one of:

- an explicitly ordered, unique `domains` list; or
- `all_authorized=true`, which walks registry order and omits domains the
  caller cannot read.

Private domains additionally require `include_private=true`, an authenticated
`principal_id`, and a stored domain grant. Explicit unauthorized requests fail
with `unauthorized`; all-authorized discovery records the omission in the
scope receipt. Unknown explicit domains fail with `unknown_domain`. A backing
that becomes unavailable during fan-out is recorded as a typed partial failure
while healthy-domain results survive.

Cross-domain search compares per-backing *rank*, not raw relevance values. Its
reciprocal-rank-fusion score is only a deterministic ordering aid and is never
described as a probability. Mixed embedding models are therefore supported but
reported as incompatible in the scope receipt. Shared documents are returned
once with every domain/backing retrieval path attached.

The governed schemas are `noesis-cross-domain-request-v1` and
`noesis-cross-domain-response-v1`. Cross-domain answers also validate as
`noesis-answer-v1`; their outer `domain` is the reserved string `cross-domain`, and the complete domain
scope lives in the evidence plan.

`kb_answer` is additive, so it does not require `noesis-kb-v2`. Its nested
payload has its own `answer_contract: noesis-answer-v1` discriminator and
schema. Existing calls and response shapes are unchanged.

`kb_temporal` is additive for the same reason. Its nested payload carries the
`noesis-temporal-v1` discriminator. Exact clock precedence, interval boundaries,
normalization, migration, and revision semantics are governed by
[`noesis-temporal-v1.md`](noesis-temporal-v1.md).
