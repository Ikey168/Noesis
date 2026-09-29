# Humanitarian Response and Conflict Events

The `humanitarian` bundle (provider `humanitarian.core`, tracking #2206) takes a
place, a country or a named crisis to a cited dossier of what humanitarian and
conflict-monitoring bodies published about it: ReliefWeb situation reports and
appeals, the ReliefWeb crisis entry, HDX datasets with their update history and
HXL tags, and UCDP conflict events with each coder's precision codes and release
history. Every item carries its source, source identifier, revision and as-of
time. Counts and event codings are quoted from the publishing body, and a place
with no record simply has none on record.

It never estimates casualties, never merges or sums counts across coders, never
scores or forecasts conflict risk, never ranks severity or gives operational
advice, and never stores personal data about affected people. ACLED is not
acquired: its licence decision is `declined` (see the
[source audit](../development/humanitarian-evidence/source-audit.md)).

## Enabling

The bundle is authored natively (`packs/humanitarian/manifest.json`, alias
`humanitarian-response`). It requires its own six capabilities plus
`geospatial.place-resolution`, `geospatial.spatial-relation`,
`geospatial.geometry-store`, `osint.corroboration`, `news.articles`,
`economics.demographics`, `platform.entity-identity`, `platform.subscriptions`
and `platform.source-acquisition`. The optional `acled` feature is off by
default and documents what an accepted licence decision would enable.
`humanitarian_bundle_status` reports per source whether it is ready, fixture-only,
stale, unavailable or declined; live verification is reported separately.

Install and enable the `humanitarian-response` source pack
(`config/source_packs/humanitarian.json`), accept each source's licence and run
the sources (`acquire_humanitarian_source`, or the source-pack runtime). Import
the COD-AB admin boundaries you use for place queries with
`import_humanitarian_admin_boundaries`; the boundary vintage is kept on every
place and every place match.

## Components

| Part | Owner |
| --- | --- |
| Source contracts, ACLED decision, adapters (ReliefWeb, HDX + HXL, UCDP GED/Candidate, gated ACLED) | `src/ingestion/humanitarian_sources.py`; audit in `docs/development/humanitarian-evidence/source-audit.md` |
| Record contract (`noesis-humanitarian-record-v1`) and validation | `src/kb/humanitarian_records.py`, `contracts/schemas/jsonschema/noesis-humanitarian-record-v1.json` |
| Append-only revisions, receipts, provider state, source-pack projector | `src/kb/humanitarian_store.py` |
| Reviewable identity (places, crises, organisations, actors) | `src/kb/humanitarian_identity.py` on `GeospatialStore`, `canonical_entities` and `EntityHistoryStore` |
| Citation links (news, OSINT dossiers, population series, hazard events) | `src/kb/humanitarian_links.py` |
| Answers and conflict-event queries | `src/kb/humanitarian_queries.py` |
| Monitors | `src/kb/humanitarian_monitoring.py` on `SubscriptionStore` |
| Bundle, dossiers and evidence-bundle export | `src/kb/humanitarian_bundle.py` |
| MCP tools | `tools/knowledge_engine_mcp/humanitarian.py` |

## Records and revisions

- **Revisions append.** A changed report, dataset or event is a new revision
  linked to its predecessor with the fields that changed. A re-observed
  revision adds nothing. As-of answers use the revision with the latest
  provider change time (`as_of`) not after the requested date, or with
  `basis="retrieved"` the latest one retrieved by then, and name the revision
  used.
- **Reports** keep publishing organisations, report date, formats and country
  and disaster tags exactly as ReliefWeb lists them. Bodies are referenced by
  URL and never mirrored.
- **Datasets** keep licence, access flags, resources with their hashes and the
  HXL hashtags of each resource's header row. Untagged columns are marked
  `untagged`, and contact tags are flagged. HDX Connect, private and non-open
  datasets are metadata-only, and none of their resources is read.
- **Conflict events** keep the coding source (`ucdp-candidate`, `ucdp-ged`),
  the dataset version, the coder's `where_prec`, `date_prec` and violence type
  as `{code, scheme, label}`, actor labels and `best`/`low`/`high` counts as
  published. A final GED release of a candidate event is a revision of the same
  event. A complete final release that no longer contains a candidate records a
  `dropped-in-release` revision, never a deletion.
- **Personal data** is rejected anywhere in a record. UCDP headlines, article
  text and `where_description` are dropped at parse time.

## Identity

`propose_humanitarian_identity` offers assertions with method, evidence and
confidence: exact p-code or ISO3 against the imported COD-AB places, normalised
admin names, a GLIDE number shared by a ReliefWeb disaster and an HDX dataset
tag, canonical-entity aliases for organisations and actor labels, and identical
organisation names across sources. A different principal with
`knowledge:humanitarian:review` accepts or rejects each one with a reason, and
can revert it. The decision is an entity identity decision. Records are never
rewritten, and anything without an accepted assertion stays visible as
unmatched.

## Answers

- `humanitarian_dossier` returns the reports, appeals, crisis entries and
  dataset revisions about a place (by p-code, ISO3 or place id) or a crisis as
  of a date. Place answers reach records only through accepted place
  assertions, walk the admin hierarchy, and report the boundary vintage. Every
  answer lists the sources consulted, ACLED under `sources_declined` as
  "not acquired (licence)", and every gap as "none on record".
- `query_humanitarian_conflict_events` takes a bounding box of at most 5 × 5
  degrees or one admin place, and a window of at most 366 days. It returns each
  coder's events side by side with their precision codes and counts as
  published. The `imprecise` policy (`exact-only`, `admin`, `all`) states
  whether admin-level and country-level events are included, and excluded
  events are listed.
- `humanitarian_event_history` returns every release revision of one event
  with the fields that changed.
- `export_humanitarian_dossier` renders the dossier as a
  `noesis-evidence-bundle-v1`. Every record revision is cited with its source,
  revision and as-of time. Declined sources, gaps and broken links are
  omissions.

## Links

`cite_humanitarian_link` links a record revision to a news article, an OSINT
event dossier or a population series that the source cites, with the citation
locator. `attach_humanitarian_population` attaches a population denominator,
with its vintage, by citation or through an accepted place match; no
per-capita value is computed. GLIDE numbers link crises to Natural Hazards
events (#2207) when that bundle publishes them. Missing targets are kept as
broken links with their reason.

## Monitoring

`create_humanitarian_monitor` subscribes to a place or crisis. For events it
also takes a bounded area and window. `run_humanitarian_monitor` evaluates the
newest committed watermark and notices new reports, report revisions, new or
revised datasets, new events and event releases. Each notice cites the revision
and names the changed fields. Repeated runs are idempotent. Notices are record
changes, not alerts.

## Evidence

The offline journey is `tests/unit/domains/test_humanitarian_acceptance.py`.
Unit tests are in `tests/unit/humanitarian/`. The authored fixtures use
synthetic identifiers, dates in 2098–2099 and fixture actor labels. Live
coverage is unverified until a dated run is recorded in
`docs/development/humanitarian-evidence/` (HR14, #2293).
