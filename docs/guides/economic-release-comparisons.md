# Economic release snapshots and comparisons

The economic tools compare retained dataset vintages. A snapshot pins one release label, the publication cutoff, the local acquisition cutoff, selected series, exact observations, source revision when retained, and the declared provider release ID. The provider release ID is caller supplied and labeled as such; a URL or release date alone does not establish that Noesis possessed an earlier vintage.

Each series also carries `release_at_basis`, `retrieved_at_basis`, `vintage_basis`, and `release_time_status`. A `provider_vintage_fallback` means the provider did not expose a usable official release timestamp and the retained provider vintage is used for cutoff selection. Snapshot limitations repeat this qualification. Connector-specific clocks and coverage gaps are described in the [economic provider vintage guide](economic-provider-vintages.md).

After ingesting two vintages of a series, call the MCP tools with exact millisecond cutoffs:

```json
{"tool":"create_economic_release_snapshot","arguments":{"namespace":"economics","request_key":"gdp-aug","release_id":"GDP August 2025","release_cutoff_ms":1754006400000,"acquired_cutoff_ms":1754006400000,"series":[{"series_id":"fred:GDPC1:US","provider_release_id":"GDP-2025-08"}]}}
```

Create a second snapshot with its own `request_key`, release ID, and later cutoffs. Then compare the returned IDs:

```json
{"tool":"compare_economic_release_snapshots","arguments":{"namespace":"economics","request_key":"gdp-aug-sep","left_snapshot_id":"economic-snapshot:…","right_snapshot_id":"economic-snapshot:…","precision":2,"assumptions":["Values use their captured scaling and declared seasonal adjustment."]}}
```

`inspect_economic_release_snapshot` and `inspect_economic_release_comparison` accept `offset` and `limit` (at most 50). `export_economic_release_comparison` returns an immutable structured artifact, a hash, Markdown, source citations and calculation receipts. Same-period revisions, new-period changes, added observations and removed observations are separate. A null value yields a missing-value count and no numeric delta. Incompatible units, adjustment or methodology block arithmetic until a conversion names every addressed dimension and records its method, multipliers and evidence reference.

`create_economic_comparison_report` creates a versioned authored report when every numeric finding has retained source document revisions. It cites both source revisions and its calculation receipts, and links the source dependencies to `assess_authored_report_changes`. Earlier authored exports remain available under their original report revision. If a source revision was never retained, the authored report returns `source_revision_unavailable`; the derived comparison export remains available with its explicit limitations.

Supply `source_revision_id` in each series selector when its linked source document has a committed revision. The tool verifies the revision belongs to that document and existed by the acquisition cutoff. It never guesses which document revision supports an old vintage.

All tools require the current namespace and economic scopes, and linked source documents require their current document read scope. These operations use retained local data; fixture examples and arithmetic checks do not amount to live-provider validation or independent human review.
