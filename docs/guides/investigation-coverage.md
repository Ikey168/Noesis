# Investigation coverage

Coverage assessments compare an explicit requested scope with historical source-pack
versions and run receipts. The denominator is the cross product of the requested
date windows, geographies, languages, and topics, capped at 64 cells. A cell is
**observed** only when the caller pins a committed document revision from the
bound run with matching language and date metadata. The topic and geography
assignment remains a declared research judgment; source receipts cannot prove
complete recall.

Run a bounded source-pack search first. For each source, record its pack ID,
version, source ID, run ID, and the cell IDs from the requested scope. Use
`inspect_source_pack_run` to obtain the committed receipt and exact document
revisions. Then call `save_coverage_assessment`:

```json
{
  "namespace": "research",
  "request_key": "coverage-2026-09-23",
  "scope": {
    "dates": [{"from_ms": 1735689600000, "to_ms": 1767225600000}],
    "geographies": ["global"],
    "languages": ["en"],
    "topics": ["causal inference"]
  },
  "bindings": [{
    "pack_id": "research-discovery",
    "pack_version": "1.2.0",
    "source_id": "crossref-works",
    "run_id": "source-run:PINNED_RUN_ID",
    "cell_ids": ["coverage-cell:ID_FROM_SCOPE"],
    "evidence": {
      "coverage-cell:ID_FROM_SCOPE": [{
        "document_id": "spdoc:PINNED_DOCUMENT_ID",
        "revision_id": "PINNED_REVISION_ID"
      }]
    }
  }]
}
```

Use `inspect_coverage_assessment` for paginated source explanations,
`compare_coverage_assessments` to distinguish changed scope or pack versions
from changed evidence, and `export_coverage_comparison` for report-ready cells,
reasons, receipts, and limitations. A zero-result run is shown as
`empty-successful` only when a pinned source capability declares all four query
forms and the stored run request exactly matches the cell with a covering
backfill window. Otherwise it is `unavailable` with `query_scope_unverified`.
Even a verified empty search does not assert that the topic has no evidence
elsewhere. Failed receipts can explain gaps but are marked `failed_uncommitted`;
only committed document revisions count as observed evidence. Unattempted
sources remain separate. A preflight reason may be
attached with its observed time and manifest hash; stale readiness is identified
after 48 hours. Credential values are never accepted.

The [cross-pack template](../../config/investigation_templates/cross-pack-coverage.json)
shows a two-pack project outline. The tests use offline public-provider fixtures.
Provider recall, credential availability, topic fit, and human review require
independent verification for a live investigation.
