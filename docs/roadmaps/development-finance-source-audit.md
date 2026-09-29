# Development finance: source-contract audit and bounded provider coverage (D01)

Tracking: #1932 · delivery issue #1952 · recorded 2026-09-28.

This audit sets out, per source, what the Funding & Grants bundle's optional
`development-finance` feature may acquire, how, and on what terms. It was
written without network access. Endpoints, parameters, identifiers and terms
come from the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live documentation,
the live terms and a real response before the first dated live run (D12,
#2039). No provider is `live` until that run exists.** The machine-readable
copy of these decisions is `PROVIDER_CONTRACTS` / `LIVE_VERIFICATION` in
`src/ingestion/development_finance_sources.py` (the shape of
`src/ingestion/funding_providers.py`), and the MCP tool
`development_finance_source_contracts` returns it.

These non-goals apply to every source. Each publisher's records stay
separate: the same activity reported by two publishers is two activities, side
by side, and amounts are never totalled across publishers. CRS aggregates are
statistics, never decomposed into activities or summed with IATI transactions.
No currency is converted without a cited rate and rate date. Nothing judges
effectiveness or impact.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| IATI Datastore (api.iatistandard.org) and the IATI developer API | publisher-reported activities (IATI activity standard 2.03) | `unverified-live` | Documented search API with a free subscription key. The parser reads the IATI XML activity standard; the output endpoint (`/datastore/activity/iati`), the Solr query fields (`reporting_org_ref`, `recipient_country_code`, `sector_code`), the `start`/`rows` paging, the rows ceiling and the subscription-key header name (`Ocp-Apim-Subscription-Key`) are _verify_ |
| OECD CRS through the OECD SDMX REST API (sdmx.oecd.org) | statistical aggregates | `unverified-live` | Anonymous SDMX REST API. Acquired through the existing `SDMXConnector` (new provider `OECD`, SDMX-CSV with `format=csvfile`). The dataflow reference (`OECD.DCD.FSD,DSD_CRS@DF_CRS,<version>`), the dimension ids and order, the CSV column set, the `PRICE_BASE` codes (V current, Q constant), the base-year attribute and the recipient aggregate codes are _verify_ |
| World Bank Projects API (search.worldbank.org) | project records with the World Bank as publisher | `unverified-live` | Documented public JSON API without authentication. The parameters (`countrycode_exact`, `id`, `rows`, `os`), the field names (`boardapprovaldate`, `closingdate`, `totalcommamt`, `idacommamt`, `ibrdcommamt`, `projectfinancialtype`, `sector`, `theme_list`) and the commitment currency (reported as US dollars) are _verify_ |
| transparenzportal.bund.de | portal view | `not-implemented` | A portal over German federal development cooperation data that the ministries and implementing agencies also publish to IATI. No documented machine access is relied on and pages are never scraped. The publishers are reached through IATI Datastore selections by their reporting-org references (_verify_ the references in the IATI Registry and whether a documented export exists) |
| EU Aid Explorer (euaidexplorer.ec.europa.eu) | portal view | `not-implemented` | A portal combining EU institutions' IATI data and OECD statistics. No documented machine access is relied on and nothing is scraped. EU institutional publishers are reached through the IATI Datastore and CRS through the OECD SDMX API (_verify_ whether a documented export exists) |

The SDMX connector supported only ECB, ESTAT and BBK before this feature. The
OECD endpoint now has its own host (`sdmx.oecd.org`), dataflow-reference rule
(`AGENCY,DSD@DATAFLOW,VERSION`) and SDMX-CSV URL; each provider's SDK request
must now resolve to that provider's own host. The OECD CSV carries no
`LAST UPDATE` column (_verify_), so a CRS vintage is dated by the provider's
update stamp when present, else by the release the operator declares in the
source pack (label and publication date), else by retrieval time, labelled as
such.

**Unavailable-access fallback.** A failed page (HTTP error, redirect, a host
outside the declared set, budget exhausted, schema drift) is recorded as stale
publisher coverage with its failure code. The last revisions stay current, and
no activity is marked closed, ended or withdrawn because of a failure. Only a
*complete* selection (every page read, ended by a short page) can record that a
publisher no longer publishes an activity, and that record never means the
activity ended. A CRS failure leaves earlier vintages current.

**Live evidence.** DurableHTTP receipts state `execution: network` for a live
run and `injected` for fixture transports; datasets record `live` or `fixture`
evidence accordingly. Nothing is reported as live coverage until a dated run
is recorded in `docs/development/development-finance-evidence/`.

## Per-source contract

| Source | Access and authentication | Rate limits and pagination | Identifiers (stored as reported) | Update cadence | Retained evidence |
| --- | --- | --- | --- | --- | --- |
| IATI Datastore | HTTPS GET with the subscription key as a secret header (read from `NOESIS_IATI_DATASTORE_KEY`, never stored in request metadata) | per-key quota (_verify_ figure); `start`/`rows`, at most `max_pages` × `rows` per selection; one DurableHTTP budget per run | IATI activity identifier (`iati-identifier`), publisher = `reporting-org/@ref`, organisations by `participating-org/@ref`, `provider-org/@ref`, `receiver-org/@ref` (org-id.guide prefixes such as `XM-DAC-`, `GB-COH-`, `XI-LEI-`), DAC donor/agency codes inside `XM-DAC-` references | publishers publish on their own schedule; the Datastore re-indexes the IATI Registry several times a day (_verify_) | raw XML per page (DurableHTTP blob digest), one document per activity with the capture digest and activity index as locator |
| OECD CRS | anonymous HTTPS GET through the runtime's default transport (same-host redirects only, byte ceiling) | an hourly anonymous request limit (_verify_ figure); no paging, bounded by the declared series key and period window; at most `max_pages` declared series per run | dataflow id and version, dimension codes (DONOR, RECIPIENT, SECTOR, MEASURE, CHANNEL, FLOW_TYPE, PRICE_BASE, UNIT_MEASURE; _verify_) | annual CRS release with in-year revisions (_verify_ calendar) | raw SDMX-CSV per series key; row number per observation; each release a vintage |
| World Bank Projects API | anonymous HTTPS GET through DurableHTTP | not documented (_verify_); `rows`/`os`, at most `max_pages` per selection | World Bank project ID (`P` + six digits), country codes as reported | continuous; approvals and restructurings update records (_verify_) | raw JSON per page, one document per project |

## Bounded coverage

No record set implies complete coverage of any provider.

| Source | Selected coverage |
| --- | --- |
| IATI Datastore | the publishers (reporting-org references), recipient countries (ISO 3166-1 alpha-2) and DAC sectors named in each selection; at most 20 values per field; never "all of IATI". Coverage records store what was requested, what came back per publisher, how many pages were read and why reading stopped |
| OECD CRS | the series keys declared in `config/source_packs/economic.json` (`oecd-crs-development-finance`): one donor, the named recipients (including an aggregate such as developing countries total) and one DAC sector category, current and constant prices, a bounded period window |
| World Bank Projects API | the countries or project IDs named in each selection |

## Terms

| Source | Terms as recorded | Retention |
| --- | --- | --- |
| IATI | each publisher's licence as declared in the IATI Registry, commonly open licences (_verify_ per publisher); the Datastore's terms of use (_verify_) | activities as reported, captures and documents |
| OECD | OECD terms and conditions: reuse with attribution (_verify_ for the CRS) | vintaged cells and raw CSV |
| World Bank | Terms of Use for Datasets, CC BY 4.0 for most datasets (_verify_ for the Projects API) | project records and raw JSON |
| transparenzportal.bund.de, EU Aid Explorer | portal terms (_verify_); not acquired | none |

## Pinned fixtures

Fixtures under `tests/fixtures/development_finance/` are authored in the
documented shapes (IATI 2.03 XML, World Bank Projects API v2 JSON, SDMX-CSV)
and name fictional organisations (`XM-DAC-99901` Fictional Development
Partnership Agency, `XI-IATI-FICTNGO` Fictional NGO Alliance, `GB-COH-99000001`
Fictional Water Works Ltd), activities, projects (`P999001`-`P999003`) and
amounts; the CRS rows use real code shapes with fictional values. The CRS
source-pack fixture (`tests/fixtures/source_packs/economic-development-finance-oecd-crs.json`)
is pinned with its digest and expected output hash in
`config/source_packs/economic.json` (`economic-statistics-and-filings` 1.4.0,
which keeps every 1.3.0 source verbatim, so the Economics pins `^1.3.0` and
`^1.1.0` still resolve). Fixtures are never evidence of live coverage.
