# Integrity ledger

Noesis keeps provenance checks as a ledger, not an opaque “trust score.” The
`kb_integrity` MCP/REST call assembles one view per document from six existing
subsystems:

- ingest-time URL snapshots and live/archive citation state;
- staged re-fetch history and disclosed corrections, retractions, takedowns,
  cosmetic changes, and silent substantive edits;
- content-addressed image appearances and perceptual-hash reuse;
- EXIF claims and locally verified C2PA content credentials;
- prose-versus-figure quantitative contradictions;
- evidence locators back to the document, archived snapshot, or asset
  appearance that supports each finding.

A silent edit always carries two locators—previous and current revision—with
their hashes and fetch times. This is enough to reproduce what changed rather
than merely trusting a “changed” flag. Every result follows the analytics
honesty envelope (`n`, `method`, `assumptions`). Missing C2PA is neutral;
automated reuse/cross-modal findings request review and do not assert fakery.

```text
MCP:  kb_integrity(domain="web3", document_id="doc-123")
REST: GET /api/v1/kb/web3/integrity?document_id=doc-123
```

`kb_diff` and the daily brief include the same ledger findings for documents
that arrived in the requested window, so integrity changes travel beside the
claims they qualify.
