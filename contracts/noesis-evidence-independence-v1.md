# noesis-evidence-independence-v1 — probable reporting-origin graph

This additive contract separates the number of publications from the number
of probable reporting origins. It never turns dependency inference into an
accusation or a truth score.

The governed schema is
`contracts/schemas/jsonschema/noesis-evidence-independence-v1.json`.

## States and durable representation

`reporting_origins` stores current origin nodes. `document_origin_links`
contains exactly one current link per document and method version, enforced by
its primary key. `document_origin_link_history` is append-only and retains the
evidence behind earlier merge/split decisions. Every current or historical
link records:

- `known_independent`, `likely_dependent`, or `unknown`;
- the inference method and version;
- a confidence interval, reason codes, decisive signals, and as-of time;
- a nullable origin id, because unresolved provenance must be representable.

Every publication remains an independent evidence object even when several
documents share one probable origin.

## Signals and precedence

`document_origin_signals` exposes decisions inputs separately. V1 extracts
normalized canonical URLs, exact normalized-content hashes, hashed text
fingerprints, bylines, datelines, explicit wire/upstream attribution, outbound
source links, ownership metadata, shared-media hashes, quoted-passage markers,
publication time, press-release markers, and explicit original-reporting
metadata.

Canonical identity and exact content are decisive. Matching explicit upstream
attribution plus story overlap is next. Near-duplicate text requires a
calibrated threshold and, below the strongest similarity band, corroborating
provenance such as a shared byline, link, media hash, or quotation. Conflicting
explicit upstream signals block similarity-only merging. Ownership alone is
non-decisive. Similarity can only produce `likely_dependent`, never
`known_independent`.

## Counts and compatibility

Origin-aware corroboration reports:

- publication count;
- probable-origin count;
- known-independent and likely-dependent counts;
- unresolved count;
- the active method, assumptions, and dependency evidence.

`independent_source_count` remains as a compatibility field. It contains the
probable-origin count when lineage has been materialized. If the lineage tables
are absent, it retains distinct normalized-source behavior and names the method
`distinct-source-fallback-v1`.

KB Answer, `kb_corroborate`, the KB MCP tool, the OSINT MCP tool, and the REST
corroboration route all consume the same repository functions. Source
reliability includes lineage as a separate component and does not fold it into
a single reliability or truth score.

## Migration, backfill, and repair

The migration is additive and idempotent on empty or populated DuckDB
warehouses. Run one restart-safe batch at a time:

```console
python scripts/backfill_evidence_independence.py \
  --db-path data/neuronews.duckdb --batch-size 100
```

`origin_backfill_progress` exposes processed, remaining, status, cursor, and
last run id. Already extracted `(document_id, signal_version)` rows are skipped,
so retry and restart are safe. Each batch recomputes current components;
incremental reruns converge without duplicate current links.

V1 retains signals, run records, origins, and link history indefinitely. A
merge keeps the deterministic smallest component root. A split or changed
threshold rebuilds the current link table, deactivates orphaned origin nodes,
and preserves previous relations in history. There is no destructive rollback:
forward repair means correcting input metadata or the method version and
rerunning extraction/inference.

## Offline evaluation

`scripts/evaluate_evidence_independence.py` calibrates only on
`development.json`, freezes the selected threshold (`0.78`), then evaluates
`final.json`. False independence has three times the error cost of false
dependency. The committed fixtures cover exact and canonical copies, rewrites,
press releases, wire syndication, independent reports, shared ownership,
shared media/quotes/links, papers, filings, and unknown provenance.

The deterministic final partition contains 9 documents and 4 evaluated pairs:
pairwise precision and recall are both 1.0 (`n=3` predicted/expected dependent
pairs; 95% Wilson interval 0.4385–1.0), cluster exact match is 1.0 (`n=5` cases;
95% Wilson interval 0.5655–1.0), and the one expected unknown is retained.
