# Economics extractives and natural resources guide

The Economics pack's `economics.extractives` provider (#2653) answers two
questions: what did a company, its group or a country's extractive sector pay
governments according to each EITI report version, and what mineral and
hydrocarbon production and reserves did each statistics publisher report for a
commodity and a country, as of a date? It covers EITI summary data, the USGS
Mineral Commodity Summaries and the BGS World Mineral Statistics. Each source is
its own optional feature (`extractives-eiti`, `extractives-usgs`,
`extractives-bgs`), all off by default.

Figures are stored as published. EITI government-reported and company-reported
amounts are shown side by side with the discrepancy the report itself states,
in the currency as reported; currencies are never converted and nothing is
summed across reports. USGS and BGS series are separate series, shown side by
side and never blended; withheld values stay withheld and estimated or revised
values stay marked. Nothing reconciles discrepancies beyond the reports,
estimates reserves, scores corruption or governance risk or forecasts prices.

Source contracts, the personal-data minimisation decision and the bounded
coverage are in the [source audit](../development/extractives-evidence/source-audit.md).
The audit was written without live access to the providers; no provider is
live until a dated run verifies it (#2717).

## Enable it

Select one or more of `features: ["extractives-eiti", "extractives-usgs",
"extractives-bgs"]` in the Economics bundle composition. Each binds
`economics.extractives`, `platform.subscriptions` and
`platform.source-runtime`; `extractives-eiti` also binds
`platform.entity-identity`. The sources ship in the separate source pack
`economic-extractives` 1.0.0 (`config/source_packs/economic-extractives.json`);
the Economics bundle's `economic-statistics-and-filings` pin does not change.
Ownership, trade, Energy, public-finance and infrastructure records are not
required: links to them report `provider_absent` when their stores are not
held.

## Acquire

Install the source pack and run its sources through the source-pack runtime
(`noesis-extractives-record-v2` pages are projected by
`src.kb.extractives_store.ExtractivesProjector`). Each declared document is one
publication:

- an EITI summary-data document names its country, fiscal period and the
  report version with its publication date; a revised report is declared as a
  new version;
- a USGS MCS table names its commodity, the meaning of each value column
  (statistic, year, unit, estimated) and the operator-declared ISO and M49
  codes of each published country name;
- a BGS query names its commodity, the statistic types it maps and the country
  codes.

A release is never truncated: a run whose result budget is smaller than a
release fails with `budget_exhausted`. Replays add nothing; changed content under
an unchanged version is refused (`vintage_conflict`) and an older report version
arriving after a newer one is refused (`stale_version`).

## Ask

| Tool | Answers |
| --- | --- |
| `query_extractives_company_payments` | payments of a company (and with `group=true` its group as of the date) per EITI report version and revenue stream, through reviewed company matches; `all_versions=true` lists each version's payments |
| `query_extractives_country_payments` | a country's reports, versions, revenue streams with government-reported totals and payments with each company's match status |
| `query_extractives_production` | a commodity (or `hs:<heading>` through accepted concordance matches) and a country to production and reserves per source and vintage |
| `extractives_record_history` | every revision of an EITI record with its report version |
| `export_extractives_evidence_bundle` | an evidence bundle whose assertions cite source, record revision and as-of time |

"No payment on record" is never a clean bill: only acquired report versions and
reviewed matches are searched. Similar-name unmatched companies are listed as
unknowns on request and never counted.

## Review identity

- Companies: `propose_extractives_company_matches` offers EITI companies to the
  legal entities of an ownership namespace, published identifiers first (a KvK
  number is the GLEIF `RA000463` number, a Companies House number the
  `RA000585` one), equal names as low-evidence candidates. A reviewer accepts,
  rejects or reverts each (`review_extractives_company_match`,
  `revert_extractives_company_match`). Individual payers are never offered.
- Commodities: `import_extractives_concordance` records the publisher's
  commodity-to-HS correspondence with its citation;
  `propose_extractives_commodity_matches` proposes matches from it only.
- Projects: `propose_extractives_project_matches` proposes infrastructure
  assets that share a published identifier or published coordinates with a
  project. Names are never matched.

Nothing is merged: records stay as published and unmatched records stay
visible.

## Link

`link_extractives_trade_flows` (accepted HS heading and published country
code), `link_extractives_energy` (shared SIEC and country code),
`link_extractives_infrastructure` (accepted project match) and
`link_extractives_public_finance` (explicit citation) record the basis and the
subject and target revisions of every link. Linked values are listed side by
side, never combined. `list_extractives_links` shows them, including the
`provider_absent` and `target_not_found` attempts.

## Monitor

`create_extractives_monitor` subscribes to companies, countries or commodities.
`run_extractives_monitor` reports new and revised EITI report versions,
new, revised and removed payments (with amounts before and after) and new or
revised commodity releases, each citing the record revision and release. Notices
are record changes, not assessments; unchanged releases and restarts emit
nothing.

## Personal data

Contact persons, beneficial owners and signatories are never parsed. A payer the
report marks as an individual keeps its payments with its name withheld and its
identifier dropped. The store refuses personal fields at write time and every
tool checks its answer before returning it.

## Evidence

Offline: `tests/unit/domains/test_extractives_*.py`, including the acceptance
journey `test_extractives_acceptance.py`, replay authored fixtures of fictional
companies and figures with sockets blocked. Live: none yet (#2717). Offline
coverage is never reported as live coverage.
