# Economics extractives guide

Given a company (and its group), a country or a commodity, what have the
sources published about extractive-sector payments to governments and about
mineral and hydrocarbon production and reserves, in which report version or
release vintage, as of a date? The Economics bundle's optional
`extractives-eiti`, `extractives-usgs` and `extractives-bgs` features (#2653)
answer from EITI summary data, USGS Mineral Commodity Summaries and BGS World
Mineral Statistics. No new pack exists: the `economics.extractives` provider
lives in `packs/economics/`, and commodity values and vintages live in the
Economics series storage (`economic_indicators`, `economic_series_map`,
`economic_vintages`, `dataset_observations`); `ex_*` tables hold report
revisions, payment and discrepancy lines, series metadata and value flags.

Exclusions: no reconciliation of payment discrepancies beyond those EITI
reports, no own reserve estimates, no corruption or governance risk scoring, no
price forecasts, no currency conversion or sums across reports, no blending of
USGS and BGS series, no filled withheld values, no inferred project ownership.

## Enable and acquire

Select any of the three features in the composition plan (for example
`features: ["extractives-eiti", "extractives-usgs"]`); each binds
`economics.extractives`, `platform.subscriptions` and
`platform.source-runtime`. Corporate Ownership, Trade, Energy, public-finance
and infrastructure stores are used when held and degrade to unmatched or
`provider_absent` when not. Acquisition runs through the
`economic-statistics-and-filings` source pack (1.7.0): `eiti-summary-data`,
`usgs-mineral-commodity-summaries` and `bgs-world-mineral-statistics`. Every
provider is `unverified-live` until a dated live run
([source audit](../development/extractives-evidence/source-audit.md)).

## Records

- **EITI report revision**: country and fiscal period, report label and
  version, currency; a changed or withdrawn summary is a new revision.
- **Payment line**: government agency, revenue stream (GFS code as reported),
  company and project as reported, who reported it (government or company),
  amount with the currency the report states.
- **Discrepancy**: the report's own government and company figures, the
  discrepancy and its explanation.
- **Commodity series**: source, commodity and form, statistic (production,
  reserves, capacity, imports, exports), unit and country; one vintage per USGS
  release or BGS publication with new, revised and removed years.
- **Observation**: value text, status (reported, withheld, not available,
  symbol only), estimated and revised flags as published.

Contact persons are never stored and natural-person entities are redacted
(EX01 minimisation decision).

## Identity and links

`propose_extractives_company_matches` offers reporting companies to ownership
entities (published identifiers first, names as low evidence); a reviewer
accepts or rejects with `review_extractives_company_match`, and reverts are
recorded as entity-identity decisions. `import_extractives_concordance` and
`propose_extractives_identity` map commodities to HS codes (a stated HS code,
else a cited table), country names to ISO codes (a published code, else a cited
code list) and projects to infrastructure assets (a published identifier or
coinciding published coordinates). `link_extractives_records` links payments to
public-finance budget lines, commodities to trade series, hydrocarbon series to
Energy series and projects to infrastructure assets; each link records its basis
and target revision, and missing providers or targets stay unresolved.

## Ask

`extractive_payments_for_company` takes an extractives company key or an
ownership entity (with `ownership_namespace` and `group=true` for its group) and
returns every matched company's payments per EITI report revision in force at
`as_of_ms`, government- and company-reported figures side by side with EITI's
discrepancies, each citing its report revision. `extractive_payments_for_country`
lists a country's reports. `commodity_production_side_by_side` takes a
commodity (name or `{hs_code}`) and a country (name or ISO alpha-3) and returns
each source's vintage current at the date, withheld and estimated values
marked. `export_extractives_evidence_bundle` cites every item with source,
record revision and as-of time. `create_extractives_monitor` subscribes to a
company, country or commodity and notifies new reports, report revisions and new
or revised commodity releases.

The offline journey is `tests/unit/domains/test_extractives_acceptance.py`.
