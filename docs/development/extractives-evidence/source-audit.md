# Extractives: source audit and bounded provider coverage (EX01)

Tracking: #2653 · delivery issue #2657 · recorded 2026-09-30.

This audit sets out, per source, what the Economics bundle's optional
`extractives-eiti`, `extractives-usgs` and `extractives-bgs` features may
acquire, how, and on what terms. **The official pages could not be read from
this runtime on 2026-09-30: the egress proxy blocked `eiti.org`,
`www.usgs.gov`, `www.sciencebase.gov`, `www.bgs.ac.uk`, `ogcapi.bgs.ac.uk`,
`www.data.gov.uk` and `energydata.info`.** What follows is recorded from web
search-result summaries of those pages (read the same day) and is marked
_unverified_ wherever it states an endpoint, a field name, a limit or a term.
Every such item must be checked against the live page, the live terms and a
real response before the first dated live run (EX13, #2717). No provider is
`live` until that run exists. The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `LIVE_VERIFICATION`, `MINIMISATION` and
`BOUNDED_COVERAGE` in `src/ingestion/extractives_sources.py`; the MCP tool
`extractives_source_contracts` returns them, and each source entry in
`config/source_packs/economic.json` carries its
`extractives.live_verification` status.

Non-goals for every source: no reconciliation of payment discrepancies beyond
those the EITI report publishes, no own reserve or resource estimates, no
corruption or governance risk scoring, no price or production forecasts, no
currency conversion and no sums across reports, no blending of USGS and BGS
series, no filled withheld values and no inferred project ownership.

## Pages consulted (2026-09-30)

| Page | Read | What was recorded |
| --- | --- | --- |
| https://eiti.org/open-data | blocked; search summary only (_unverified_) | summary data is published in a standardised open format covering government revenues, company payments, reporting entities and project-level data; downloadable as CSV per country and served through an API |
| https://eiti.org/how-we-collect-and-publish-eiti-summary-data | blocked; search summary only (_unverified_) | summary data files are imported to eiti.org and available as CSV or through the API; revenue streams are classified by the IMF GFS framework; commodities by HS code, currencies by ISO code |
| https://eiti.org/guidance-notes/eiti-summary-data-template | blocked; search summary only (_unverified_) | template parts for reporting entities (government agencies, companies, projects) and company- and project-level data by revenue stream; version 2.1 of the template applies from January 2026 |
| https://eiti.org/document/18610 (open data policy guidance) | blocked; search summary only (_unverified_) | open data should be licensed for any reuse; the concrete licence of eiti.org summary data was not found |
| https://www.usgs.gov/centers/national-minerals-information-center/mineral-commodity-summaries | blocked (_unverified_) | none directly |
| https://www.sciencebase.gov/catalog/item/677eaf95d34e760b392c4970 and https://www.sciencebase.gov/catalog/item/6798fd34d34ea8c18376e8ee | blocked; search summary only (_unverified_) | MCS 2025 data release; world production, capacity and reserves table `MCS2025_World_Data.csv` with a metadata XML, published 2025-01-31 |
| https://www.bgs.ac.uk/mineralsuk/statistics/world-mineral-statistics/ | blocked (_unverified_) | none directly |
| https://www.data.gov.uk/dataset/fb34d76e-346f-4fbb-be43-4080625e1e5d (BGS WMS WFS) | blocked; search summary only (_unverified_) | WFS 2.0 over the archive from 1970; Open Government Licence; acknowledgement "Contains British Geological Survey materials © UKRI [year]" |
| https://ogcapi.bgs.ac.uk/collections/world-mineral-statistics | blocked; search summary only (_unverified_) | OGC API Features collection; item properties include `bgs_commodity_trans`, `country_trans`, `year`, `erml_commodity` and a statistic type |

## Access decisions (`LIVE_VERIFICATION`)

| Source | Access | Authentication and keys | Rate limits | Decision |
| --- | --- | --- | --- | --- |
| EITI summary data (eiti.org) | JSON API `https://eiti.org/api/v1.0/summary_data` (_unverified_ path, filters, paging and field names); CSV per country page | none stated (_unverified_) | none published (_unverified_); each run is bounded by the declared documents and `max_pages` | `unverified-live` |
| USGS Mineral Commodity Summaries (ScienceBase) | annual data-release files (CSV plus metadata XML) on `www.sciencebase.gov` (_unverified_ file URL pattern) | none | none published (_unverified_); one request per declared file | `unverified-live` |
| BGS World Mineral Statistics (ogcapi.bgs.ac.uk) | OGC API Features, GeoJSON items (_unverified_ property names and paging); WFS 2.0 at `ogc2.bgs.ac.uk` also served | none | none published (_unverified_); one page per declared request, refused (never truncated) when `numberMatched` exceeds the page | `unverified-live` |

No source needs a key, so no secret is declared (`auth.kind` is `none`). No
source's terms, as far as they could be established, forbid the intended use;
no source is recorded as not implemented. Should the live EITI licence turn out
to restrict redistribution, the `extractives-eiti` feature must be recorded as
not implemented before EX13.

## Reuse terms

| Source | Terms | Attribution recorded per release |
| --- | --- | --- |
| EITI | EITI open-data policy expects open licensing; the licence of eiti.org summary data is _unverified_ | the report label, version and URL |
| USGS | USGS-authored data are generally US public domain; cite the data release (_unverified_) | the data-release label and publication date |
| BGS | Open Government Licence; "Contains British Geological Survey materials © UKRI [year]" and a link to the OGL where possible (_unverified_) | `structure.attribution` of each BGS release |

## Updates, corrections and removals

| Source | Update signal | Correction or removal | Vintage or revision |
| --- | --- | --- | --- |
| EITI | report publication date in the summary (`report.published`, _unverified_), else the summary's change stamp, else the retrieval time (labelled) | a re-published summary for the same country and fiscal period with changed content is a new report revision; a summary stating `withdrawn` is a revision recording the withdrawal; lines absent from a revision stay in the earlier one | report revision keyed by country and fiscal period |
| USGS | annual release (late January / early February), declared per document | a year re-stated in a later release is a revised value; a year the later table no longer states is a removed year; the earlier vintage keeps both | one vintage per annual release |
| BGS | publication (World Mineral Production edition), declared per document with its request window | a figure changed in a later edition is a revised value | one vintage per publication |

## Value conventions

- USGS: `W` withheld to avoid disclosing company proprietary data, `NA` not
  available, a trailing `e` a published estimate and `r` a published revision,
  a separate estimated column where the release has one (_unverified_ against
  each release's metadata). Withheld and unavailable values carry no number and
  are never filled; aggregates such as "World total (rounded)" stay separate
  rows and are never mapped to a country.
- BGS: a missing quantity is `not_available`; units are stored per series as
  published.
- EITI: amounts keep the text and the currency the report states; the
  discrepancy is the report's own figure with its explanation.

## Personal data: minimisation decision

EITI summary data names legal entities; some files also name contact persons
and, for artisanal or individual licence holders, natural persons.

| Aspect | Decision |
| --- | --- |
| Stored | legal-entity names and identifiers as reported, government agencies, revenue streams, projects, amounts with currency, discrepancies and explanations |
| Excluded | contact persons, e-mail addresses, telephone numbers, signatories, beneficial-owner names and any other natural-person field (dropped by the parser; only the excluded field paths are recorded) |
| Redacted | a reporting entity the summary flags as a natural person keeps its payment lines with the name replaced by `[natural person - redacted]` and no identifiers; it is never proposed for identity matching |
| Retention | as long as the report revision it came from is retained |
| Access | `knowledge:extractives:read` with namespace access; no personal field is stored, and every MCP answer is checked before it leaves the tool |

The store refuses any item carrying a natural-person key or an unredacted
natural-person entity (`personal_data_refused`), so the decision holds at write
time as well as in answers.

## Bounded first coverage

| Dimension | Selection | Why |
| --- | --- | --- |
| Countries | Peru (EITI, USGS, BGS), Chile (USGS, BGS; not an EITI implementing country) | one country with payments and production from all three sources, one production-only country for side-by-side series |
| Commodities | copper (mine production, reserves, exports), lithium (withheld US production), crude petroleum (BGS, Energy link) | covers withheld values, estimates, imports/exports and a hydrocarbon |
| Companies | the reporting companies of the declared EITI summaries | matched to ownership entities only by review |
| Periods | two most recent fiscal periods per EITI country; two most recent MCS releases and BGS editions | shows a revision and a new vintage |
| Caps | `max_results` items per response and `max_pages` requests per run; a response over the budget is refused, never truncated | a missing item would read as unpublished |

Commodity-to-HS mappings use the HS code the EITI summary states, else a
published table an operator imports with its citation (for example the tariff
items of an MCS commodity chapter); country names map to ISO 3166-1 alpha-3
through a published code list (UN M49) unless the source publishes the code.
