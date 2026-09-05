# Authored reports

Use the knowledge-engine MCP tools `create_authored_report`,
`inspect_authored_report`, `revise_authored_report`, `export_authored_report`, and
`reopen_authored_report` to keep authored wording and evidence links across revisions.
Calls require current `knowledge:reports:read` or `knowledge:reports:write`, report
ownership, report namespace access, and read access to every evidence/snapshot
namespace. Historical exports recheck current access.

Create with a namespace, idempotent request key, and this content shape:

```json
{
  "title": "Policy findings",
  "snapshot": {"id": "snapshot-17", "generations": {"papers": 7}},
  "sections": [{
    "id": "findings", "title": "Findings", "assertions": [{
      "id": "investment", "text": "The reported investment value increased.",
      "kind": "sourced", "citations": ["paper-1"],
      "dependencies": [{
        "kind": "source", "id": "paper-1", "revision": "revision-3",
        "namespace": "papers",
        "locator": {"document_id": "paper-1", "revision_id": "revision-3", "start": 120, "end": 208}
      }]
    }]
  }],
  "bibliography": [{"id": "paper-1", "text": "Author. Investment observations. 2026."}],
  "limitations": ["Association does not establish causation."]
}
```

Dependencies accept source, claim, calculation, entity, and artifact references.
`revision` is the authoritative revision identity represented as text. Assertions
without evidential support use `kind: commentary`. The export labels commentary
and makes clear that a source link is not independent verification of support.
Snapshot IDs/generations are references, not renewable access tokens or retention
guarantees. Unknown fields, dangling bibliography entries, duplicate stable IDs,
invalid coordinates, and oversized reports fail validation.

Revision calls supply the entire authored content and `expected_revision`.
Concurrent changes fail with `revision_conflict`; earlier revisions remain
reproducible. Reuse existing section/assertion IDs when editing their wording.
Export returns structured report JSON, Markdown, and the ordered authored
bibliography. Reopening verifies the structured report hash and preserves its
content in a new report ledger; derived Markdown is regenerated from that content.
The hash detects corruption and does not authenticate the author. Nothing is
published externally.
