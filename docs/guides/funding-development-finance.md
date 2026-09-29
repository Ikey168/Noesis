# Funding & Grants: development finance and aid activities

The optional `development-finance` feature of the Funding & Grants bundle
(provider `funding.development-finance`, default off) takes a funder, a
recipient country or region, a sector or an organisation to a cited set of aid
activities. Each activity is shown as each publisher reports it, with its
version history, transactions, participating organisations, sector and
geography allocations and result indicators. OECD Creditor Reporting System
aggregates appear beside it with the vintage used, and World Bank projects are
linked where a stated identifier allows. Per-publisher coverage is part of
every answer.

It never totals amounts across publishers, never converts a currency without a
cited rate and date, never infers an activity from a statistic and never judges
effectiveness or impact.

## Enabling

Select the feature on the Funding & Grants root through the composition
coordinator (`features: ["development-finance"]`). It requires its own three
capabilities plus `economics.knowledge`, `geospatial.place-resolution`,
`ownership.identity`, `platform.entity-identity` and
`platform.source-acquisition`. With the feature off the bundle's bindings are
unchanged, and Public Procurement's reuse of `funding.core` is unaffected.
`development_finance_readiness` reports whether the feature is selected, which
`devfin_*` stores exist and how many datasets each provider has.

## Components

| Part | Owner |
| --- | --- |
| Source contracts and parsers (`PROVIDER_CONTRACTS`, IATI 2.03 XML, World Bank JSON, CRS SDMX-CSV) | `src/ingestion/development_finance_sources.py`; audit in `docs/roadmaps/development-finance-source-audit.md` |
| Record owner (`noesis-development-finance-record-v1`) | `src/kb/development_finance.py`: publishers, datasets, activities and revisions, participating organisations, transactions, allocations, results, publisher coverage and withdrawals, CRS cells and vintages, World Bank projects |
| Acquisition | `src/kb/development_finance_acquisition.py` (IATI Datastore and World Bank through `DurableHTTP`); the CRS through the `oecd-crs-development-finance` source of `economic-statistics-and-filings` 1.4.0 and the SDMX connector's new `OECD` provider |
| Normalisation | `src/kb/development_finance_normalise.py`: DAC and IATI code lists as ontology modules, place resolution, cited currency conversions |
| Identity and open calls | `src/kb/development_finance_identity.py` on the shared `OwnershipIdentityService` |
| Answers | `src/kb/development_finance_queries.py` |
| Monitors | `src/kb/development_finance_monitoring.py` on `SubscriptionStore` |
| MCP tools | `tools/knowledge_engine_mcp/development_finance.py` |

## Publishers, revisions and coverage

- **Publishers stay separate.** The publisher is the IATI reporting
  organisation, a record of its own. The same IATI identifier reported by two
  publishers is two activities, and every answer lists them side by side with
  each value's revision. Fields that differ are listed under `conflicts`.
- **Revisions.** A changed activity is a new revision. The revision in force
  follows the publisher's own `last-updated-datetime`. A late-arriving older
  version lands as history, an unchanged re-acquisition adds nothing, and a
  reversion under a newer stamp is a new revision. As-of answers use the
  revisions observed by the date and select each publisher's revision in force
  before any filter is applied.
- **Unknowns stay unknown.** A transaction keeps its value text, its currency
  (stated, or the activity default, marked which), its value date and its type
  as reported. Without a value, a currency or a value date its amount is
  `unknown`, and totals list such amounts as excluded.
- **Coverage.** Each IATI or World Bank selection records what it requested,
  what came back per publisher, how many pages it read and why reading
  stopped. A failed or partial refresh is stale coverage: the last revisions
  stay current. Only a complete selection can record that a publisher no longer
  publishes an activity (`withdrawn-by-publisher`), and that never means the
  activity ended.

## Statistics and World Bank projects

- **OECD CRS** cells (donor, recipient, sector, flow, channel, price basis
  with base year) are statistics with the OECD as publisher. Every declared
  release is a vintage, and earlier vintages stay addressable as of their date.
  Recipient codes are classified by structure: DAC regional and "unspecified"
  codes and aggregate codes are aggregates, never countries. Cells are never
  decomposed into activities or summed with IATI transactions. To see them in
  an activity answer, name the CRS codes explicitly (`crs_recipient`,
  `crs_donor`). An IATI country code is never converted into a CRS code.
- **World Bank projects** are the World Bank's own records. A project links to
  an IATI activity only when the World Bank's IATI publication, a
  `related-activity` or an `other-identifier` states the project ID. A shared
  country with a similar title or an equal amount is a `candidate`, never a
  link. The two records keep their own identifiers and revisions, and their
  amounts are never summed.

## Normalisation

`publish_development_finance_codelists` publishes the bundled DAC and IATI
code-list subsets as ontology modules, versioned by release (verify against the
published lists before relying on completeness).
`normalise_development_finance_activities` writes side records for each
publisher's current revision. These records hold labels from the published
modules, `unknown-code` for codes missing from a module, and allocation checks
(`allocation-sum-not-100` is flagged and the percentages are kept as reported).
`resolve_development_finance_places` resolves recipient countries by ISO code
to the one Geospatial place carrying it. DAC regions stay aggregates, and
free-text locations wait for a `review_development_finance_place` decision.
`convert_development_finance_amount` needs a rate, a rate date, a rate source
and a citation. It stores the exact result beside the unchanged original.

## Identity and open calls

`propose_development_finance_identity_matches` offers each organisation, as
each publisher reports it, to ownership legal entities (`GB-COH-` or `XI-LEI-`
references), to publisher records, to canonical aliases (`similar-name`, never
acceptable) and to funders of funding opportunities. Nothing is linked until a
reviewer accepts. Reverting restores the reported string. An organisation with
several pending candidates is `ambiguous` and stays unmatched. A funder query
(`funder: devfin:publisher:iati:ref:...` or an ownership key) resolves through
the accepted decisions to activities across publishers. The funder's open
calls (`development_finance_open_calls`) are a cross-reference, never
activities, transactions or commitments, and no eligibility is inferred.

## Answers, projects and reports

Read tools return each answer with a receipt (query, as-of date, store
generation, coverage). `record_development_finance_answer` stores it.
`attach_development_finance_answer` pins it in a research project, and
`development_finance_report_citation` returns the bibliography entry and
dependency for an authored report.

## Monitoring

`create_development_finance_monitor` watches publishers, funders, countries,
sectors, organisations or CRS cells through a knowledge subscription.
`run_development_finance_monitor` evaluates the newest observation as a
watermark. A restart replays the recorded watermark and emits nothing new.
Notifications are `new_activity`, `new_transaction`, `corrected_transaction` (a re-dated ref-less transaction included), `removed_transaction`,
`retracted_activity` (withdrawn by the publisher, not ended),
`new_result_posting`, `new_crs_vintage` and `coverage_change`. Each one cites
the revision or vintage before and after. A failed refresh is a stale
`coverage_change`, never a closed activity.

## Evidence

Offline evidence is `tests/unit/domains/test_development_finance_acceptance.py`
together with `tests/unit/funding/test_development_finance_*.py`, run on
authored fixtures with fictional organisations. Live checks, once run, are
recorded in `docs/development/development-finance-evidence/`. Every provider is
`unverified-live` until then.
