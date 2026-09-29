# Development finance expansion scope

Status: planned, 2026-09-27. An expansion of the existing Funding & Grants
bundle; it does not create a separate domain or pack. It never offers an
effectiveness or impact judgement, never double-counts across publishers, and
never converts a currency without a cited rate and date.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1952–#2039 are open.
- Composition dependency: a `funding.development-finance` provider descriptor
  and `development-finance` optional feature (default false) in the Funding &
  Grants bundle (`packs/funding-grants/`), composed with `funding.core`,
  `economics.knowledge` and the SDMX dataset connector,
  `geospatial.place-resolution`, `ownership.identity`,
  `platform.entity-identity` and the shared research-project, authored-report,
  subscription and source-acquisition providers.

Tracking: [#1932](https://github.com/Ikey168/Noesis/issues/1932).

## Outcome

Given a country, sector, organisation or funder, assemble published aid
activities with their publishers, transactions, participating organisations,
sector and geography allocations, result indicators and version history.
OECD CRS flows appear as vintaged aggregates and World Bank projects are
linked to IATI activities where identifiers allow. Publisher records stay
separate, coverage per publisher is explicit, and unknown amounts, dates and
organisations stay unknown.

## GitHub implementation issues

- [ ] [#1952](https://github.com/Ikey168/Noesis/issues/1952) — D01 Audit development-finance source contracts and select bounded provider coverage.
- [ ] [#1967](https://github.com/Ikey168/Noesis/issues/1967) — D02 Define activity, participating-organisation, transaction, result-indicator, allocation and publisher-coverage records with revisions.
- [ ] [#1985](https://github.com/Ikey168/Noesis/issues/1985) — D03 Acquire IATI activities and transactions with publisher coverage and version history.
- [ ] [#2003](https://github.com/Ikey168/Noesis/issues/2003) — D04 Acquire OECD CRS flows through the existing SDMX connector with vintages.
- [ ] [#2013](https://github.com/Ikey168/Noesis/issues/2013) — D05 Acquire World Bank projects and link them to IATI activities where identifiers allow.
- [ ] [#2028](https://github.com/Ikey168/Noesis/issues/2028) — D06 Normalise currencies, DAC sector codes and country and region references without merging publishers.
- [ ] [#2032](https://github.com/Ikey168/Noesis/issues/2032) — D07 Match implementing and funding organisations to entity identity through reviewable decisions and cross-reference open funding calls.
- [ ] [#2035](https://github.com/Ikey168/Noesis/issues/2035) — D08 Answer who funded what where as of a date with cited activities and publisher coverage.
- [ ] [#2036](https://github.com/Ikey168/Noesis/issues/2036) — D09 Monitor new activities, transaction updates and result postings through subscriptions.
- [ ] [#2037](https://github.com/Ikey168/Noesis/issues/2037) — D10 Register the `funding.development-finance` provider descriptor and optional feature in the Funding & Grants manifest.
- [ ] [#2038](https://github.com/Ikey168/Noesis/issues/2038) — D11 Add offline funder-to-activities acceptance coverage.
- [ ] [#2039](https://github.com/Ikey168/Noesis/issues/2039) — D12 Validate live coverage and publish a cited aid-activity demo.

Order: D01 → D02 → {D03, D04, D05} → D06 → D07 → D08 → D09 → D10 → D11 → D12.

## Sources

- [IATI Datastore](https://iatistandard.org/en/iati-tools-and-resources/iati-datastore/)
  and the [IATI developer portal](https://developer.iatistandard.org/):
  publisher-reported activities, transactions, participating organisations,
  allocations and results with dataset versions.
- [OECD Creditor Reporting System](https://www.oecd.org/en/data/datasets/creditor-reporting-system-crs.html)
  through the [OECD SDMX REST API](https://sdmx.oecd.org/public/rest/):
  statistical aggregates by donor, recipient, sector and flow, kept as
  vintages; the existing SDMX connector supports ECB, ESTAT and BBK today and
  needs the OECD endpoint added.
- [World Bank Projects](https://projects.worldbank.org/en/projects-operations/projects-home)
  (API): project records with their own identifiers, linked to IATI
  activities only through stated identifiers.
- [transparenzportal.bund.de](https://www.transparenzportal.bund.de/) and
  [EU Aid Explorer](https://euaidexplorer.ec.europa.eu/): portal views of
  German and EU aid; each receives a documented access decision and is
  `not-implemented` with reason if no supported machine access exists.

D01 verifies documented access, terms, authentication, rate limits,
identifiers and cadence per provider and records an `unverified-live` or
`not-implemented` decision for each. These links establish source candidates,
not guaranteed APIs or complete live coverage. A published activity shows
what a publisher reported; it never shows that aid was effective.

## Expansion of existing capabilities

**Funding & Grants:** store aid activities, transactions, participating
organisations, result indicators, allocations and publisher coverage as their
own revision-addressable records in `src/kb/development_finance.py`, with
IATI identifiers native and the publisher as a first-class record. Source
contracts follow the `PROVIDER_CONTRACTS`/`LIVE_VERIFICATION` shape of
`src/ingestion/funding_providers.py`. Open calls in `funding_opportunities`
are cross-referenced from a matched funder; a call is never an activity, a
transaction or a commitment, and no eligibility is inferred. Monitoring
follows `src/kb/funding_monitoring.py` over the subscription outbox.
Procurement's reuse of `funding.core` is unaffected.

**Economics and the SDMX connector:** add the OECD provider to
`src/ingestion/connectors/dataset/sdmx.py` and register the selected CRS
dataflows in `config/source_packs/economic.json`. CRS cells are statistics
with the OECD as publisher, stored per release vintage and queried as of a
date. They are never decomposed into activities and never summed with IATI
transactions.

**Geospatial:** resolve recipient countries and regions from ISO 3166 and DAC
region codes through `geospatial.place-resolution` (`src/kb/geospatial.py`).
An activity's own location elements stay distinct from its recipient-country
allocation; ambiguous or free-text places stay unresolved pending review.

**Entity identity and ownership:** propose funding, implementing and
accountable organisations as identity candidates from stated identifiers and
canonical aliases, recorded through `platform.entity-identity`
(`src/kb/entity_history.py`, `canonical_entities`) and `ownership.identity` in
the pattern of `src/kb/procurement_identity.py`. Nothing is linked
automatically; every decision is auditable and reversible, and an unmatched
organisation stays a source string.

**Ontology:** publish DAC sector, purpose, channel, flow-type and aid-type
code lists and the IATI organisation-role and transaction-type lists as
versioned ontology modules through `src/kb/ontology.py`; an unknown code stays
the reported string with a flag.

**Rights and terms:** IATI data carries publisher licences; OECD and World
Bank data carry attribution terms recorded in the source entries. Retained
evidence is bounded raw capture with locators, cited by source URL, dataset
version or vintage. Converted amounts are stored only next to the original
value, currency, value date and the cited rate and rate date.

## V1 acceptance

- Pinned IATI, OECD CRS and World Bank fixtures cover multi-publisher
  reporting of one activity, corrected and retracted transactions, a two-vintage
  CRS cell, an explicit World Bank link and a candidate-only case, an
  unconverted amount with no citable rate and an unmatched organisation.
- Repeated ingestion is idempotent; every revision keeps its publisher,
  dataset version or vintage, source URL and capture digest.
- An as-of query returns only revisions and vintages observed on or before the
  date, per publisher, with conflicts side by side and no cross-publisher total.
- Organisation matches are reviewable and reversible; funders with open calls
  show the call as a cross-reference with its revision.
- Monitoring classifies new activities, corrected transactions, result
  postings, new CRS vintages and coverage changes with before and after
  revisions; a failed refresh reports stale coverage, never an ended activity.
- Offline acceptance (`tests/unit/domains/test_development_finance_acceptance.py`)
  and bounded live checks (`docs/development/development-finance-evidence/`)
  are recorded separately per provider. The demo answers a funder or country
  question and exposes activities, publishers, revisions, transactions,
  allocations, result indicators, CRS vintages, World Bank links, coverage and
  unknowns.

## Deferred

Defer full-mirror ingestion of the IATI registry, unreviewed fuzzy matching
of organisations or of World Bank projects to activities, derived
effectiveness, results or value-for-money scores, forward-looking budget
projections, any currency conversion without a cited rate and date, and
scraping of portals without supported machine access. Publisher record counts
do not imply complete coverage of a funder, country or sector.

There is no standalone Development finance domain manifest, source-pack
manifest, enablement flag, or separate installation lifecycle in this scope.
