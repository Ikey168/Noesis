# Clinical Evidence

Given a clinical question (condition and intervention, optionally population,
comparator and outcomes), assemble the registered trials with their registry
version history, posted results, EU registry and regulatory records, and the
publications and preprints linked to them; then explain the strength of the
evidence base from recorded methodology, separately from any conclusion.
Tracker: [Ikey168/Noesis#1847](https://github.com/Ikey168/Noesis/issues/1847).

**Boundary.** The pack gives no medical advice and no dosing or treatment
recommendation. Evidence strength is described by documented rules over
recorded fields; no score is computed and no GRADE (or other) grade is asserted
when its inputs are missing. Adverse-event figures are reporting counts, never
incidence or causation. Registration, results posting and publication are
distinct kinds. The same trial in two registries is linked, never merged.
Unknown fields stay unknown. Every map and strength view carries the boundary
text (`src/kb/clinical_evidence.py: NON_ADVICE`).

The bundle reuses existing owners: the source-pack runtime (budgets, cursors,
receipts, schedules) and `DocumentStore` for acquisition, the Science paper
connectors and documents for publications, `PaperFamilyStore` and Crossref
notices for preprint/version/retraction families, `MethodologyStore` for trial
design and outcome switching, `SystematicReviewStore` for review protocols,
`OntologyAlignmentStore` and the schema registry for MeSH crosswalks, the
evidence-bundle builder for export and `SubscriptionStore` for monitoring. It
adds no scheduler, permission ledger, project store, paper store or database.

| Concern | Module |
| --- | --- |
| Source contracts, identifiers, parsers, native source-pack adapters (H01, H03-H06) | `src/ingestion/clinical_providers.py` |
| Source pack (sources, budgets, schedules, fixtures) | `config/source_packs/clinical-evidence.json`, `tests/fixtures/source_packs/clinical-*.json` |
| Record contract, store, cross-registry links, projector, schema registration (H02) | `src/kb/clinical_records.py`, `contracts/schemas/jsonschema/noesis-clinical-record-v1.json` |
| PROSPERO registrations linked to review protocols (H06) | `src/kb/clinical_reviews.py`; `SystematicReviewStore.link_registration` |
| Registry identifiers in scholarly records (H07) | `src/ingestion/connectors/paper/trial_registry.py` |
| Publication links, paper families, retractions, coverage gaps (H07) | `src/kb/clinical_publications.py` |
| Trial design and outcome switching (H08) | `src/kb/methodology_provenance.py` (`register_trial_revision`, `trial_revisions`, `outcome_switching`), `src/kb/clinical_methodology.py` |
| Evidence map, strength view, evidence bundle (H09) | `src/kb/clinical_evidence.py`, `contracts/schemas/jsonschema/noesis-clinical-evidence-map-v1.json` |
| MeSH and registry-term modules, crosswalks, expansion (H10) | `src/kb/ontology.py` (`descriptor_concepts`, `label_crosswalk`, `explain_expansion`), `src/kb/clinical_terms.py` |
| Monitoring (H11) | `src/kb/clinical_monitoring.py` |
| Bundle declaration, enablement, readiness (H12) | `src/kb/clinical_bundle.py`, `packs/clinical-evidence/manifest.json`, `packs/clinical-evidence/providers/clinical.core.json` |
| MCP entry points (knowledge-engine server) | `tools/knowledge_engine_mcp/clinical.py` |

## Sources and access decisions (H01)

`PROVIDER_CONTRACTS` records, per source, the documentation, access method,
authentication, rate limits, pagination, cadence, terms, retained evidence,
identifiers and how they cross-reference, and the fallback when access fails.
`LIVE_VERIFICATION` states what has actually been verified live (nothing yet).

| Source | Access used | Auth | Pagination / bounds | Identifiers and cross-references | Status |
| --- | --- | --- | --- | --- | --- |
| ClinicalTrials.gov | API v2 `/api/v2/studies/{NCT}` and search; history via `/api/int/studies/{NCT}/history[/{v}]` (site-internal, not in the documented v2 contract) | none | `pageToken`; one pinned study per page; at most 40 versions | NCT; `secondaryIdInfos` (EudraCT, CTIS, other registries, sponsor ids); `referencesModule` PMIDs | implemented, unverified live |
| EU CTIS | public portal JSON `ctis-public-api/retrieve/{EU CT}` (what the portal uses; not a versioned API) | none | pinned trials | EU CT number; declared NCT/ISRCTN/EudraCT | implemented, unverified live |
| EU CTR | full-text protocol download `ctr-search/rest/download/full` | none | pinned EudraCT numbers; one page may hold several member-state protocols | EudraCT; A.5.2 NCT, A.5.1 ISRCTN, A.5.3 WHO UTN | implemented (results: availability and URL only), unverified live |
| openFDA | `/drug/label.json`, `/drug/drugsfda.json`, `/drug/event.json` counts | optional `api_key` from `NOESIS_OPENFDA_API_KEY` (never stored) | `limit`; count queries return top terms | SPL set id + version, NDA/ANDA/BLA, names | implemented, unverified live |
| EMA medicines | published medicines data export (JSON) filtered to pinned product numbers | none | one file | EMA product number, INN, MeSH-labelled therapeutic area | implemented, unverified live |
| PROSPERO | no documented public API or bulk export identified | n/a | n/a | CRD | **not implemented**: user-supplied record exports only |
| WHO ICTRP | machine access by agreement with WHO | n/a | n/a | primary registry ids, UTN | **not implemented**: primary registries are used directly |
| Cochrane | licensed | n/a | n/a | n/a | **not implemented**: no licence configured |
| Europe PMC, PubMed, medRxiv/bioRxiv | existing Science sources and connectors | as configured | as configured | PMID, DOI, DataBank/accession ids, published DOI | reused |

Every implemented source runs as a native connector of the `clinical-evidence`
source pack. The runtime owns license acceptance, budgets (pages, results,
bytes, timeouts, retries), cursors bound to the pinned selection, page receipts
(each request's path, HTTP status and response SHA-256) and schedules. A failed
run marks the provider stale; it never changes or removes a record.

## Records (H02)

`noesis-clinical-record-v1` kinds: `registered-trial` (registry, identifier,
status, phase, sponsor, design, registration dates, secondary identifiers,
member-state decisions, declared references), `trial-arm`, `outcome-measure`
(primary/secondary/other, time frame, present in a version or not),
`result-posting` (as posted; `content_acquired` says whether only availability
is known), `regulatory-record` (approval, label revision, safety communication,
adverse-event summary, authorisation status; openFDA records must carry the
disclaimer; adverse-event summaries carry fixed count semantics),
`registry-link` (registry-registry, registry-publication to an exact
`documents` revision, registry-review, each with an evidence kind) and
`review-registration`.

The store follows the C01.2 rules: one authoritative store
(`ClinicalRecordStore`), content-derived ids scoped by namespace, immutable
numbered revisions, and a native-revision link on each (registry version and
date, label version, EMA revision number or observation). Each registry version
becomes a trial revision with its version date; outcome and arm records get a
revision per version, so a primary outcome's role and time frame keep their
history. Observing the same native version with different content retains both
and reports a version conflict. Validation rejects keys that would carry
advice, dosing, incidence, causation or a grade.

## Linking and methodology (H04, H07, H08)

Registry-to-registry links come from declared secondary identifiers; both
directions are kept with their locators, and `cross_registry` lists
field-level disagreements (status, phase, allocation, masking, enrolment,
sponsor, primary outcomes) with each side's record revision. Registry-to-
publication links come only from registry-declared references, secondary-source
identifiers (PubMed `DataBankList`, Europe PMC accession annotations) and paper
families. A registry number found only in text is a candidate that an
independent reviewer accepts or rejects. Unlinked trials, trial reports with no
registry identifier, declared-but-unharvested references and pending candidates
are coverage gaps.

Each registry version is registered in methodology provenance as a
`clinical-trial` study revision with phase, allocation, masking, planned and
actual sample size, pre-registration status and primary (and secondary) outcome
definitions. `outcome_switching` reports `primary-added`, `primary-removed`,
`primary-retimed`, `primary-demoted`, `publication-retimed` and
`publication-different-primary` as findings citing both sides; non-trial
studies and existing methodology tools are unchanged.

## Terms (H10)

MeSH descriptors are published as the `clinical-mesh` ontology module (tree
hierarchy kept). Registry-native terms of a namespace are published as
`clinical-terms.<namespace digest>` with a crosswalk to MeSH: exact label
matches are `equivalent`; reviewer curations keep their kind. Expansion runs
through the existing `expand` (incompatible pairs block expansion) plus the
reverse of reviewed `equivalent`/`broader` mappings, and every step is
explained. Unmapped terms and FAERS reaction terms (MedDRA, licensed) are listed
as gaps.

## Evidence map and strength view (H09)

The map lists trials (versions, design, results state by rules R1-R4,
publications, retractions, outcome-switching findings, cross-registry
disagreements, unknowns), regulatory records with disclaimers, registered
reviews and coverage gaps. Design categories (D1-D5) and the summary category
(S1-S6) are documented rules; each lists the recorded fields it used. GRADE is
never computed: the view reports which GRADE inputs are missing, or the
reviewer-recorded ratings when every domain is recorded. Argument-mining claim
conclusions are a separate view. A map pins every record revision it used and
becomes stale when any of them (or a trial's postings and links) changes until
it is recomputed. `export_bundle` writes a `noesis-evidence-bundle-v1` with
gaps and staleness as declared omissions.

## Monitoring (H11)

A clinical monitor is a knowledge subscription over one map. Evaluating it at a
committed watermark produces replay-safe events classified as trial status
change, new registry version, results posted, label revision, new linked
publication, retraction or stale source; each cites the record and its
revisions. Refreshes run only through the source pack's runtime schedule
(ownership can be claimed by the composition root) and the maintenance
orchestrator.

## Composition (H12)

`packs/clinical-evidence/manifest.json` binds `clinical.core` plus
`science.literature`, `science.methodology`, `science.systematic-reviews`,
`science.paper-families` (Science-owned descriptors under
`packs/science/providers/`), `platform.source-runtime` and
`platform.subscriptions`, with one authority per store. Enabling or disabling
is a coordinator selection change with an activation receipt
(`set_clinical_bundle_enabled`); disabling keeps Science and every shared
provider serving. The shadow report's clinical disagreements are annotated by
the reviewed rules (plan attribution and probe vocabulary).

MCP tools (`noesis-knowledge-engine.*`) and required scopes:

| Tool | Scopes |
| --- | --- |
| `clinical_provider_contracts` | none |
| `clinical_bundle_status`, `lookup_clinical_trial`, `clinical_trial_history`, `clinical_coverage_gaps`, `expand_clinical_question`, `inspect_clinical_evidence_map`, `clinical_strength_view`, `export_clinical_evidence_bundle` | `knowledge:clinical:read` |
| `clinical_outcome_switching` | `knowledge:clinical:read`, `knowledge:methodology:read` |
| `poll_clinical_monitor` | `knowledge:clinical:read`, `knowledge:subscriptions:read` |
| `import_prospero_registration`, `build_clinical_evidence_map` | `knowledge:clinical:write` |
| `link_clinical_publications` | `knowledge:clinical:write`, `knowledge:paper-family:write` |
| `record_clinical_trial_design` | `knowledge:clinical:write`, `knowledge:methodology:write` |
| `align_clinical_terms` | `knowledge:clinical:write`, `knowledge:schema:register` |
| `create_clinical_monitor`, `run_clinical_monitor` | `knowledge:clinical:write`, `knowledge:subscriptions:write` |
| `review_clinical_publication_link` | `knowledge:clinical:review` |
| `set_clinical_bundle_enabled` | `operator` |

Namespace scopes (`namespace:<ns>:read|write`) are required as well; stores
enforce them on every call. Acquisition itself is
`noesis-knowledge-engine.run_source_pack_execution` with the
`clinical-evidence` pack.

## Evidence

Offline: `tests/unit/clinical/` (per module) and
`tests/unit/domains/test_clinical_evidence_acceptance.py` (one test per
acceptance row) over the authored fixtures in `tests/fixtures/clinical`.
Live: `scripts/clinical_live_check.py`; the 2026-09-27 run
(`docs/development/clinical-evidence/live-check-2026-09-27.json`) was blocked by
the build environment's egress proxy for every host (`source_unavailable`), so
no provider is live-verified. The demo (`scripts/clinical_demo.py`,
`docs/examples/clinical-evidence-map-demo.md`) is offline and uses a fictional
intervention.
