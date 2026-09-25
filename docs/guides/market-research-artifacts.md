# Evidence-linked market research artifacts

The market research surface stores immutable, owner-scoped revisions for:

- earnings releases, presentations, transcripts, guidance, segment
  disclosures, ownership, and pre-event estimates;
- company dossiers that compare reported values with prior periods, guidance,
  and timestamp-valid consensus while separating management claims from
  verified calculations, and join segment disclosures, ownership snapshots,
  and dated peers only when their source revisions and publication/as-of clocks
  are valid at the requested cutoff;
- dated industry profiles and competitor/product/supplier/customer/geography
  relationships with asserted, extracted, or analyst-inferred provenance;
- overlap-checked top-down and bottom-up TAM/SAM/SOM models with scenarios and
  sensitivity inputs;
- industry/company driver hypotheses with alternatives and evidence that stay
  descriptive rather than silently becoming causal conclusions;
- falsifiable, versioned theses and non-destructive supporting/contradicting
  evidence reviews; and
- rights-aware JSON/Markdown/CSV briefs with pinned cutoffs, formulas,
  assumptions, source locators, linked artifact revisions, bounded chart
  specifications, and retry-safe delivery history. Every chart includes a
  bounded text alternative derived from its series and points (or supplied by
  the author); Markdown and citeproc DOCX/PDF exports include that alternative
  as ordinary heading/paragraph content, along with source-revision
  dependencies. Chart specifications carry their own source-revision IDs; they
  are renderable data, not an assertion that an image or licensed source payload
  was copied into the brief. This structural alternative is machine-tested, but
  does not replace screen-reader or analyst accessibility review of rendered
  files.

Every artifact retains an input hash, source revision locators, owner, version,
and record hash. Private, unlicensed, late, contradictory, or missing material
is labeled in the result. The five-company acceptance journey is available as
a deterministic fixture workflow, but its analyst sign-off and live-provider
evidence remain explicitly pending until performed.

`review_market_acceptance_journey` records that eventual sign-off as a separate
immutable receipt linked to the journey's record hash. The authenticated human
reviewer must attest the review, decide every usefulness criterion, provide an
explicit review timestamp for retry-safe persistence, and identify any live
evidence used. The operation never changes a pending journey into an accepted
one in place, and it cannot substitute fixture data for missing provider
evidence.

`export_market_brief` exposes the stored JSON/Markdown/CSV formats and an
optional DOCX/PDF path through the existing citeproc renderer. Formatted
rendering is an explicit capability: if the pinned renderer is not installed,
the operation returns `renderer_unavailable` rather than claiming that a file
was produced. Source locators carrying provider entitlement metadata are
rechecked for export (and redistribution when `external=true`). Internal
exports with incomplete rights remain marked `unverified`; external exports
are withheld if there are no source locators or any locator lacks a current
entitlement decision.

`generate_market_brief` can also use `compose_artifacts=true` with exact
`artifact_id`/`version` references to company dossiers, industry models,
market-sizing models, and theses. It reads those revisions through the normal
namespace/owner authorization path, rejects artifacts newer than the requested
cutoff, and stores their immutable record hashes in the brief. The generated
sections are concise summaries; charts are bounded data specifications, not
rendered images. Assumptions and source-revision locators are retained, while
private or explicitly unavailable source rows are excluded. The brief records
`generation_mode` as `artifact_composition` or `authored_sections` so generated
summaries remain distinguishable from analyst-authored text. Composition does
not replace the separate report renderer, current-rights checks, or analyst
review.

Each export includes a SHA-256 digest of the canonical payload and the
immutable artifact record hash. `verify_market_brief_export()` checks payload
consistency offline and, for JSON exports, recomputes the record hash. These
embedded hashes are not signatures: compare the artifact hash with a trusted
delivery receipt or the immutable source store when authenticity matters.

For workflows using Noesis's shared Evidence Bundle format,
`export_market_brief_evidence_bundle()` rechecks the `evidence` capability at
export time and pins the bundle to the report cutoff and immutable source
revisions. An external bundle additionally checks export and redistribution.
If a source is missing, expired, revoked, or restricted, only a rights receipt
and safe source revision identifiers are exported; report sections, chart
values, source locators, and source bytes are omitted. Offline bundle integrity
does not establish that a citation is truthful or that a provider granted
rights beyond the currently recorded policy.

Company-dossier `segment_disclosures`, `ownership`, and `dated_peers` are
optional arrays of source-linked rows. Each row needs `source_revision_id` (or
`source_revision_ids`) and `public_at_ms` (or `published_at_ms`) no later than
`cutoff_ms`. Ownership and peer rows also need `as_of_ms` (or
`observation_at_ms`) no later than the cutoff. Ineligible rows are omitted from
the dossier and persisted input, with exclusion counts recorded under
`source_coverage`; empty groups appear as explicit uncertainty gaps. These
fields provide a reproducible join surface, not live provider acquisition or
analyst validation.

## Scheduled delivery over a configured webhook

`MarketBriefDeliveryWorker` (`src/domains/market/delivery.py`) runs unattended
schedules from the maintenance worker. Enable it by starting
`scripts/knowledge_maintenance_worker.py --live-delivery` with a
`market_brief_delivery` block:

```json
"market_brief_delivery": {
  "enabled": true,
  "principal_id": "svc:market-briefs",
  "scopes": ["market:research:read", "market:research:write", "namespace:market:research:write"],
  "namespaces": ["market:research"],
  "destinations": [{"kind": "webhook", "ref": "research-team", "url_env": "NOESIS_MARKET_BRIEF_WEBHOOK_URL"}],
  "output_format": "markdown", "external": false,
  "retry_delay_s": 300, "cooldown_s": 86400, "max_per_tick": 20
}
```

Each tick does three things:

1. Runs due schedules through `run_due_schedules`, producing the same
   cutoff-pinned artifacts as manual generation.
2. Exports each brief through `export_brief`, which rechecks current rights.
3. POSTs a `noesis-market-brief-delivery-payload-v1` document to every
   destination and records the outcome in the existing delivery history.

Delivery behaviour:

- **Transport.** Destinations reuse the subscription `webhook_transport`: the
  URL comes from a `NOESIS_*` environment variable and redirects are refused.
- **Retries.** The delivery key is sent as `Idempotency-Key`. A transport
  failure becomes `retrying` and is resent with the same key once
  `retry_delay_s` has passed, so a crash between send and record can only
  cause a duplicate the receiver can recognize.
- **No double sends.** A brief that was delivered, or withheld, within the
  cooldown is not sent again.
- **Withheld exports.** If rights withhold an export (for example, an external
  delivery without verified rights), the brief is not sent and the delivery is
  recorded as terminal `withheld`.

A local end-to-end run on 2026-09-24 sent one scheduled brief to an HTTP sink
across two worker restarts. The sink received exactly one POST, and its
`Idempotency-Key` matched the recorded delivery key.

## Automated accessibility check

Every delivered payload includes the result of
`check_market_brief_accessibility(export)` (`noesis-market-brief-accessibility-v1`).
For Markdown and JSON exports it checks for:

- a single leading H1 and no skipped or empty headings;
- a text alternative for every chart;
- alt text on images;
- header rows on tables.

The result reports `not_applicable` for withheld payloads and for binary
DOCX/PDF payloads. It always records `human_review: not_performed`: it is an
automated structural gate, not a substitute for assistive-technology or analyst
review.

## Acquiring SEC company materials

`FilingsConnector.ingest_market_materials(...)` calls
`harvest_sec_company_materials` (`src/ingestion/connectors/edgar_materials.py`)
and saves the rows as a versioned `noesis-market-materials-v1` artifact.

| Kind | Source | Notes |
| --- | --- | --- |
| `earnings_release`, `earnings_presentation` | 8-K Item 2.02 EX-99.x exhibits | Linked to the reporting period, acceptance time, document URL and a SHA-256 of the fetched bytes (`source_revision_id`). Amendments carry `corrects_material_id`; byte-identical refilings carry `duplicate_of`. |
| `guidance` | Sentences in those releases with an outlook cue and a number | Stored as `management_claim` with exact character spans into the block-normalized text. The artifact's `guidance_history` lists them in publication order for later comparison. Forward-looking-statement boilerplate is excluded. |
| `segment_disclosure` | Latest 10-K/10-Q Inline XBRL | Facts on operating-segment, product/service and geographic axes, keeping the native context. |
| `insider_transaction` | Form 3/4/5 XML | Reporting owners, roles and non-derivative transactions. |
| `beneficial_ownership` | Schedule 13D/13G and DEF 14A filings | Metadata only; tables and percentages are not parsed. |
| `earnings_call_transcript`, `analyst_consensus`, `institutional_holdings_13f` | none | Saved with `licensing_status` `unlicensed`/`unavailable` and a reason. SEC does not provide them and no licensed provider is configured, so pre-event consensus is explicitly unavailable rather than inferred. |

The live sample in `config/market/acceptance_packs/live-sec-materials.json`
(re-run with `scripts/market_live_sec_evidence.py materials`) was acquired on
2026-09-24 for MSFT, ORCL, CRM, ADBE and NOW. Every issuer has four quarters of
earnings releases, and the sample also contains guidance history, segment
members, ten insider filings and ownership filings, with no diagnostics.

`build_dossier_inputs()` in `src/domains/market/dossier_inputs.py` joins those
materials to as-first-filed quarterly revenue facts. It derives a labeled
fourth quarter only as annual revenue minus the three filed quarters, compares
the active release headline with the filed fact at the headline's stated
precision, and compares a prior quarterly guidance range with the next actual.
Correction-chain heads are analyzed once, while original, amended, and
byte-duplicate material revisions remain in the dossier's citation history.
The resulting headline checks, material-change history, and structured source
gaps are persisted in the immutable dossier; unavailable consensus is never
substituted with an inferred estimate.

## Building dossiers from acquired evidence

`build_dossier_inputs` (`src/domains/market/dossier_inputs.py`) derives
`company_dossier` inputs from a saved materials artifact and filed revenue
facts. Quarterly rows come from `quarterly_revenue_from_facts`, which uses the
as-first-filed value. A fiscal fourth quarter is derived as the annual figure
minus the three filed quarters and labeled
`derivation="annual_minus_three_quarters"`.

- **Headline reconciliation.** The release's same-line total revenue figure is
  compared with the filed quarter at the release's stated precision (for
  example, `$10.24 billion` agrees within ±$5M). Sub-line figures such as
  subscription or cloud revenue, and multi-line "Targets" tables, are skipped.
  A mismatch becomes contradicting evidence.
- **Guidance vs actual.** A quarterly revenue range guided in one release is
  compared with the next reported quarter (`within_range`, `above_range` or
  `below_range`). The range stays a `management_claim` published before the
  event; the actual value and the delta are filed calculations.
- **Corrections.** Amended releases supersede the originals they correct.
  Every revision stays cited in `material_change_history`, and only the head
  of each correction chain is analysed.
- **Gaps.** Unmatched quarters, missing headlines, missing prior guidance and
  unavailable sources (transcripts, consensus) are listed, never estimated.

`scripts/market_live_sec_evidence.py dossiers` records a live run. The
2026-09-24 pack (`live-sec-dossiers.json`) reconciled every detected release
headline for MSFT, ORCL, CRM, ADBE and NOW; one ORCL release has no parseable
headline and is recorded as a gap. It also compared CRM's guided Q2 FY27 range
($11.27–11.35B) with the filed $11.345B (within range).
