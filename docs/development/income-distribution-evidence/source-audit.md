# Income, poverty and inequality: source audit and bounded coverage (IP01)

Tracking: #2583 · delivery issue #2588 · recorded 2026-09-30.

This audit sets out, per source, what the Society bundle's `society.income`
provider may acquire, how, and on what terms. **The official pages could not be
fetched from this environment on 2026-09-30**: `pip.worldbank.org`,
`ec.europa.eu`, `www.oecd.org`, `blogs.worldbank.org`, `cran.r-project.org`,
`rdrr.io` and `db.nomics.world` were all refused by the network egress proxy
(`EGRESS_BLOCKED`). Only web-search result summaries were readable; they are
cited below with the date read, and every point they do not settle is marked
_verify_. Endpoints, parameters, field names and terms marked _verify_ must be
checked against the live documentation, the live terms and a real response
before the first dated live run (IP13, #2648). **No provider is `live` until
that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and
`MINIMISATION` in `src/ingestion/income_distribution_sources.py`; the MCP tool
`income_source_contracts` returns them, and each source entry in
`config/source_packs/society.json` carries its
`income_distribution.live_verification` status.

Non-goals for every source: figures are stored as each source published them.
Nothing is nowcast, no year a source did not publish is filled, no poverty line
of our own is set, PIP, EU-SILC and OECD figures are never blended into one
series, welfare concepts, equivalence scales and PPP rounds are never
re-harmonised, and no indicator is derived that no source published.

## Sources read (2026-09-30)

| Reference | How it was read | What it settles |
| --- | --- | --- |
| https://pip.worldbank.org/api | fetch refused (egress); web-search summary only | the API exists at this address; parameters `povline` (up to three decimals, default 2.15), `fill_gaps` (default false), `ppp_version` (YYYY), `version`, `release_version` (YYYYMMDD); a current version string of the form `20260922_2021_01_02_PROD` and 2021 PPP prices in the current release (search summaries of the `pipr`/`worldbank` R client documentation) |
| https://datacatalog.worldbank.org/search/dataset/0063646/poverty-and-inequality-platform-pip-percentiles, https://www.worldbank.org/en/about/legal/terms-of-use-for-datasets | web-search summary only | PIP is licensed CC BY 4.0 under the World Bank dataset terms (_verify_ on the PIP site) |
| https://ec.europa.eu/eurostat/web/income-and-living-conditions/database | fetch refused (egress); web-search summary only | `ilc_li02` at-risk-of-poverty rate by poverty threshold, age and sex (threshold 60 % of national median equivalised disposable income after social transfers); `ilc_di12` Gini coefficient of equivalised disposable income; `ilc_di03` median equivalised income; dimension order and codes _verify_ |
| https://www.oecd.org/en/data/datasets/income-and-wealth-distribution-database.html | fetch refused (egress); web-search summary only | the IDD is updated on a rolling basis two to three times a year; Data Explorer dataflow `OECD.WISE.INE` `DSD_WISE_IDD@DF_IDD`; dimensions poverty line, reference area, measure, statistical operation, unit of measure, age, methodology (two options), definition (three options); measure `INC_DISP_GINI`; income concept household disposable cash income under the 2012 terms of reference (`idd-tor-2012-onwards.pdf`) |

## Access decisions (`LIVE_VERIFICATION`)

| Source | Access | Authentication and keys | Rate limits | Decision |
| --- | --- | --- | --- | --- |
| World Bank PIP (`api.worldbank.org/pip/v1`, _verify_ base path) | JSON; `/pip` country estimates, `/pip-grp` regional aggregates, `/versions` release list (_verify_) | none | none published found (_verify_); bounded by declared documents, `max_pages` 3, `max_results` 50 | `unverified-live` |
| Eurostat EU-SILC (`ec.europa.eu`, SDMX 2.1 dissemination API) | SDMX-CSV with `LAST UPDATE` and `OBS_FLAG`, read by `SDMXConnector.parse_csv` (ESTAT path) | none | none published (_verify_); bulk asynchronous extractions out of scope; `max_pages` 4 | `unverified-live` |
| OECD IDD (`sdmx.oecd.org`, .Stat Suite SDMX REST) | SDMX-CSV `format=csvfile`, read by `SDMXConnector.parse_csv` (OECD path) | none | about 20 data queries a minute per IP as recorded by the labour audit (_verify_); `max_pages` 1 | `unverified-live` |

No source needs a credential, so no secret variable is declared; the adapters
refuse any request not on the endpoint's documented host.

## Reuse terms

- **PIP:** CC BY 4.0 under the World Bank terms of use for datasets
  (search summary, 2026-09-30; _verify_ on the PIP site). Attribution to the
  World Bank Poverty and Inequality Platform with the release version.
- **Eurostat:** reuse with attribution under the Eurostat copyright policy
  (Commission Decision 2011/833/EU), as already recorded for the Eurostat
  sources of the demographics and labour tracks; _verify_ for EU-SILC.
- **OECD:** reuse with attribution under the OECD terms and conditions
  (`https://www.oecd.org/en/about/terms-conditions.html`), as recorded by the
  labour track; _verify_ for the IDD.

No source's terms, as far as they could be read, forbid the intended use
(storing published aggregates with citation), so no source is recorded as not
implemented. Should the live terms say otherwise, the source's
`access_decision` becomes `blocked` and the feature stays unselected.

## Updates, corrections and removals

| Source | Release signal | Revision model |
| --- | --- | --- |
| PIP | the release version string `{YYYYMMDD}_{PPP year}_{PPP revision}_{data revision}_{identity}` (layout _verify_); the pinned version dates the vintage | every PIP release is its own vintage; a changed PPP-revision component or changed per-value `ppp` factor with restated values is a `ppp_revision`; a change of PPP round changes the poverty line and so the series |
| EU-SILC | `LAST UPDATE` per dataset response | a changed response with a new `LAST UPDATE` is a new vintage; `OBS_FLAG` letters (`b` break, `p` provisional, `e` estimated, `u` low reliability, `c` confidential, `d` definition differs) are stored verbatim |
| OECD IDD | dataflow version in the `DATAFLOW` column; otherwise the retrieval time (labelled) | a changed response or version is a new vintage; a version change is a definition change; `OBS_STATUS` break flags (`B`, _verify_) become source-stated breaks |

For every source, a period the source no longer states is recorded as
`removed_periods` of a new vintage, and a series a document's response no
longer states gets a `withdrawn` vintage. Nothing is deleted.

## Data minimisation (`MINIMISATION`)

The three sources publish aggregate statistics; none of the declared requests
returns person- or household-level data. The decision:

- **Stored:** published aggregate values with their flags, labels and notes;
  survey metadata as published (survey acronym, survey year, coverage, welfare
  type, estimation label); release, retrieval and file digests.
- **Excluded:** EU-SILC, LIS and national survey microdata (research-contract
  access, out of scope); PIP percentile or distribution files beyond the
  declared aggregate indicators; any field naming or identifying a person,
  household or respondent.
- **Redacted:** nothing, because no personal field is admitted. A document,
  response or record carrying one is refused at write time
  (`personal_data_refused`, `check_item` and the adapters), and MCP outputs are
  checked again before they are returned (`minimised`).
- **Retention:** every retained vintage is provenance and kept; removals by the
  source are revisions.
- **Who may query:** `knowledge:income:read` with namespace read access; writes
  need `knowledge:income:write`, reviews `knowledge:income:review`.

## Bounded first coverage (`BOUNDED_COVERAGE`)

| Source | Places | Indicators | Periods | Caps |
| --- | --- | --- | --- | --- |
| PIP | Germany (income, survey years), Indonesia (consumption, reference-year estimates with PIP's estimation labels), Sub-Saharan Africa (regional aggregate) | headcount and poverty gap at 2.15 PPP$ (2017 PPP), Gini, median and mean welfare, top-decile share | two to four recent reference years | 3 requests, 50 series per response |
| EU-SILC | Germany, Austria, Berlin (NUTS 2 DE30) | at-risk-of-poverty rate (60 % of median), Gini, single-person at-risk-of-poverty threshold | from the declared `startPeriod` | 4 requests, 50 series per response |
| OECD IDD | Germany, United States | Gini and poverty rate at 50 % of the median under the 2011 and 2012 terms of reference | declared period window | 1 request, 50 series per response |

Justification: Germany is covered by all three sources and carries the
side-by-side journey; Indonesia and the regional aggregate cover consumption
welfare, PIP's reference-year labels and aggregates; Berlin covers NUTS
matching; the United States covers a non-EU OECD country. The caps keep each
run within the documented budgets and the source-pack ceilings.

## Series keys and definitions

A series is keyed by source, indicator, welfare concept (income or
consumption), equivalence scale (per-capita for PIP, modified OECD for
EU-SILC, square root for the OECD IDD), poverty line and its PPP base year,
reference-year basis (survey year; PIP reference-year "lineup"; income year),
survey, coverage, methodology, area and unit. EU-SILC's survey year and income
reference year (the previous calendar year for most countries, declared per
document, _verify_ per country) are both stored per value. OECD values are never
mixed with EU-SILC values even where the underlying survey is the same.

## Implementation

All three sources are read through existing code: EU-SILC and the OECD IDD
through `src/ingestion/connectors/dataset/sdmx.py` (`SDMXConnector.parse_csv`,
unchanged), PIP through the `income-distribution` connector's JSON reader, which
reads numbers as exact text. Fixtures under `tests/fixtures/source_packs/society-income-*.json`
and `tests/fixtures/income_distribution/` are authored in the documented shapes
with fictional values and 2096-2099 reference years. The EU-SILC `LAST UPDATE`
stamps are dated 2026 so that a runtime run on the real clock never sees a release
dated after its retrieval (the Economics series storage refuses that); the PIP
release versions are dated 2099 and are replayed with 2099/2100 retrieval clocks.
