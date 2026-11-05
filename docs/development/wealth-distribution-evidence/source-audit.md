# Wealth distribution: source-contract audit and bounded coverage (WD01)

Tracking: issue #2882 ([Society] Add wealth distribution statistics) · recorded
2026-11-02.

This audit sets out, per source, what a `society.wealth` provider in the
existing Society bundle (`packs/society/`, beside `society.income` and
`society.social-protection`) may acquire, how, and on what terms. **It was
written without access to the publishers: `wid.world`, `www.federalreserve.gov`,
`data-api.ecb.europa.eu`, `www.ecb.europa.eu`, `www.bundesbank.de` and
`api.statistiken.bundesbank.de` did not resolve from this runtime's egress proxy
(checked 2026-11-02).** A web search (result snippets only, no publisher page
read) confirmed some facts, noted as _search_ below; everything else comes from
the publishers' documentation as the author knows it. Every item marked
_verify_ must be checked against the live pages, the live terms and a real
response before the first dated live run. No source is `live` until that run
exists, and no licence term below is asserted as verified.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS`, `PACING` and `LIVE_VERIFICATION` in
`src/ingestion/wealth_distribution_sources.py`; it must match this audit. The
source-pack entries `wid-wealth-distribution`,
`ecb-distributional-wealth-accounts` and `fed-distributional-financial-accounts`
live in the `society-wealth-distribution` pack
(`config/source_packs/society-wealth-distribution.json`) and carry the same
`live_verification` status. Request shapes, dimension ids, column names and
flag attributes in the fixtures are the author's reading of the documentation
and stay _verify_.

Non-goals for every source: no nowcasting, no filled or interpolated periods of
our own, no blending of WID, DWA, DFA, HFCS or PHF figures into one series, no
re-harmonising of wealth concepts, population units or percentile groups, no
top shares, Pareto fits, averages, ratios or other indicators of our own, no
person-, household- or rich-list-level data, no forecasts. Model-based
estimates (WID, DWA, DFA) are stored as the publisher's estimates with the
publisher's method and flags; they are never presented as observed survey
values and never corrected by us.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `wid-wealth-distribution` | World Inequality Lab, World Inequality Database (WID.world) | top 1%, 0.1% and 0.01% net personal wealth shares; national and private wealth-to-income ratios | the WID API that WID's own R and Stata tools call (`countries-variables` on an AWS API Gateway host, JSON, `x-api-key` header, _verify_ host, path, response shape and whether WID permits third-party use of the key); the bulk archive `wid_all_data.zip` (_search_: one `WID_data_XX.csv` and `WID_metadata_XX.csv` per country) exceeds the runtime's 100 MB response budget (_verify_ its size) and is not used | `unverified-live` |
| `ecb-distributional-wealth-accounts` | European Central Bank, Distributional Wealth Accounts (DWA, experimental statistics) | quarterly household net wealth by wealth group (top 10%, bottom 50%) and median net wealth | ECB Data Portal SDMX REST API, `https://data-api.ecb.europa.eu/service/data/DWA/{key}?format=csvdata` (_verify_ the format parameter and column layout); dataflow `DWA`, key positions `FREQ.REF_AREA.REF_SECTOR.ACCOUNTING_ENTRY.STO.INSTR_ASSET.DWA_GRP.UNIT_MEASURE.DWA_EST.DWA_COMP` (_search_: series such as `DWA.Q.DE.S14.N.LE.NWA.B50.EUR.S.N`; the dimension ids are the author's labels, _verify_) | `unverified-live` |
| `fed-distributional-financial-accounts` | Board of Governors of the Federal Reserve System, Distributional Financial Accounts (DFA) | quarterly US household net worth shares of the top 0.1%, the rest of the top 1% (99-99.9), the next 9% (90-99), the next 40% (50-90) and the bottom 50% | the DFA download archive `https://www.federalreserve.gov/releases/z1/dataviz/download/zips/dfa.zip` (_verify_ path), member `dfa-networth-shares.csv` (_verify_ name and columns `Date`, `Category`, `Net worth`, ...; categories `TopPt1`, `RemainingTop1`, `Next9`, `Next40`, `Bottom50`, _verify_) | `unverified-live` |
| `ecb-hfcs-statistical-tables` | European Central Bank, Household Finance and Consumption Survey (HFCS) | per-wave aggregate tables (median and mean net wealth, wealth shares by country) | published only as PDF statistical tables per wave and data version (_search_: `HFCS_Statistical_Tables_Wave_2021_June_2026.pdf`, data version 4.1, and the July 2023 v3.3 edition); no machine-readable aggregate endpoint known (_verify_ whether the ECB Data Portal carries HFCS aggregates); HFCS microdata are excluded | `unverified-live`, format `hfcs-pdf-tables` (WD02): the declared rows of the declared table for the declared country columns only, read from PyMuPDF word positions; each value keeps its printed token, page, table anchor and quoted line; any cell short of one exact reading fails the document |
| `bundesbank-phf-aggregates` | Deutsche Bundesbank, Panel on Household Finances (PHF) | wave-level German household wealth distribution aggregates | published in Monthly Report articles with a data annex PDF (_search_: `2023-04-vermoegensbefragung-data.pdf` for the 2021 wave); no machine-readable aggregate endpoint known (_verify_ the Bundesbank time-series database for PHF series); PHF microdata (Research Data and Service Centre, scientific use only) are excluded | `unverified-live`, format `phf-pdf-tables` (WD02): the declared rows of the declared annex table, one column per printed survey year, German number notation declared per table; same reading rules as HFCS |

No source was found whose terms forbid the intended use (store published
aggregates with citations, show them side by side, export cited evidence
bundles), but no source's terms were read live. Should the live check find
otherwise, the source moves to `blocked` in `LIVE_VERIFICATION` and queries
report it as unavailable rather than empty.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| WID | `https://rfap9nitz6.execute-api.eu-west-1.amazonaws.com/prod/countries-variables?countries=...&variables=...` (_verify_; the host the WID R tool calls as the author recalls it) | an API key in the `x-api-key` header, read from the secret reference `NOESIS_WID_API_KEY` (never committed; _verify_ whether WID issues keys or permits use of the key distributed with its tools) | WID.world terms of use; reuse with citation of WID.world is common practice (_search_: Our World in Data republishes WID data citing WID.world as source); licence not verified (_verify_) | none published (_verify_); one request per declared document | no update stamp in the response (_verify_): a changed response is a new release dated by the declared WID update, else retrieval time (labelled `retrieval_time`); WID revises whole histories when its DINA methods change, so each release is a vintage and the stated method is a definition revision; years WID's metadata lists as extrapolated keep publication status `extrapolated` (_verify_ the `extrapolation` metadata field) |
| ECB DWA | `https://data-api.ecb.europa.eu/service/data/DWA/{key}?format=csvdata&startPeriod=...` (_verify_) | none | ECB statistics reuse policy: reuse with source acknowledgement (_verify_) | none published (_verify_); one request per declared document | quarterly releases revise earlier quarters (model re-estimation and new HFCS waves); no update stamp in the CSV (_verify_): declared release date, else retrieval time; `OBS_STATUS` (`A` normal, `E` estimated, `P` provisional, _verify_) and `OBS_CONF` (`C` confidential, a status never a value) verbatim; a series a later complete release no longer states becomes a `removed_by_source` vintage |
| Fed DFA | `https://www.federalreserve.gov/releases/z1/dataviz/download/zips/dfa.zip` (_verify_) | none | US federal government work; the Board's website usage policy asks for attribution (_verify_) | none published (_verify_); one request per declared document | released with each quarterly Z.1 Financial Accounts release; every release can revise the whole history (new Z.1 data, new triennial SCF wave); declared Z.1 release date dates the vintage, else retrieval time; quarters between SCF waves are the Board's model-based interpolation and keep that method statement |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, a missing archive member, a response larger than
the budget) fails that source's run with its code and a receipt; earlier
vintages stay current and nothing is marked removed or revised because of a
failure. Readiness reports the source as `stale`.

## PDF tables (WD02): ECB HFCS and Bundesbank PHF

Both publishers release their survey-wave aggregates only as PDF tables, so
`src/ingestion/wealth_distribution_sources.py` reads them with a declared,
fail-closed table reader (`read_pdf_tables`), following the precedent of the
Market insurance SFCR reader (`src/ingestion/insurance_sources.py`).

| Source | Entry point | Authentication | Licence and redistribution | Revision model |
| --- | --- | --- | --- | --- |
| ECB HFCS | `https://www.ecb.europa.eu/home/pdf/research/hfcn/HFCS_Statistical_Tables_Wave_2021_June_2026.pdf` (_verify_) | none | ECB copyright; reproduction permitted with the source acknowledged (_verify_) | each data version of a wave is a new PDF and a new release dated by its declared publication date; a changed figure is a new vintage, never an overwrite |
| Bundesbank PHF | `https://www.bundesbank.de/resource/blob/908924/.../2023-04-vermoegensbefragung-data.pdf` (_verify_; the search result was served from `bundesbank.de`, the pack declares `www.bundesbank.de`) | none | Bundesbank publications: reproduction permitted provided the source is stated (_verify_) | each Monthly Report data annex is a release; a later annex that restates an earlier wave appends vintages |

**Reading rules.** A document declares the PDF path and, per table (at most
six), an anchor text that must occur exactly once (optionally on declared
pages), the printed columns in order, the columns it reads, the rows it reads
by their printed label, the decimal and thousands separators, the markers that
mean "not published" or "confidential", and footnote flag suffixes. Text is
rebuilt into printed lines from PyMuPDF word positions. A table section runs
from its anchor to its end anchor or the next table heading (at most three
pages). A declared row must match exactly one line of the section whose
remainder is only numbers in the declared notation or declared markers, and
that line must hold exactly one cell per printed column. Anything else (the
anchor missing or repeated, a row missing or repeated, a different number of
cells, an undeclared marker, a non-PDF response, an encrypted PDF, too many
pages, PyMuPDF not installed) fails the document with its reason; the earlier
vintages stay current. No cell is guessed, re-ordered, transcribed by hand or
read outside the declaration; standard-error lines, undeclared rows and
undeclared columns are never read.

**Citation per value.** The printed token (e.g. `106.600`, `56,3`, `320.3*`),
the PDF page, the table anchor, the row label and the quoted line are kept as
observation attributes beside the normalised value. A declared marker is a
status, never a value; a footnote flag is kept beside the value.

**Unverified.** The publishers' hosts did not resolve from the build
environment, so table numbers and titles, row labels, column order, markers,
footnote conventions and the survey-year labelling are not checked against
the published PDFs. The pack's declarations are fixture declarations (survey
years 2093-2099) that a live run must replace with the published layout.
HFCS reference years differ by country within a wave (_verify_ per wave).

## Definitions recorded per series

A series is keyed by source, dataset, indicator (`wealth_share`,
`wealth_level`, `wealth_average`, `wealth_median`, `wealth_income_ratio`),
wealth concept, group, unit, population or unit of analysis, method, place and
frequency; every field carries the code as published.

- **WID:** the variable code split as WID defines it (type letter, five-letter
  concept, percentile, age code, population code): `shweal` net personal wealth
  share, `wwealn` net national wealth to net national income, `wwealp` net
  private wealth to net national income (_verify_ codes); percentile groups
  `p99p100`, `p99.9p100`, `p99.99p100`; population `992_j` adults aged 20 and
  over, equal-split between spouses, `999_i` all individuals (_verify_). Shares
  are fractions of 1 as published, never rescaled to percent. Method: WID's
  distributional national accounts (DINA) estimates combining tax, survey,
  rich-list and national-accounts data; the per-series method statement WID
  publishes is kept verbatim.
- **ECB DWA:** the instrument (`NWA` net wealth), the wealth group (`D10` top
  decile, `B50` bottom half, `_Z` whole population), the unit as published
  (share of total net wealth, euro median), households (`S14`) as the unit of
  analysis, and the method: HFCS distributions aligned to household-sector
  national accounts (model-based, experimental statistics).
- **Fed DFA:** the balance-sheet column (`Net worth`), the wealth percentile
  group as published, the unit (percent share of the aggregate), US households
  as the unit of analysis, and the method: the triennial Survey of Consumer
  Finances distributed onto the Financial Accounts (Z.1), interpolated between
  survey years (model-based).
- **Comparability:** WID personal wealth per equal-split adult, DWA net wealth
  per household and DFA net worth per household (with different wealth groups,
  balance-sheet scopes and treatment of pensions and durables) are different
  measurements. They are shown side by side with their definitions, never
  merged. A pair of series has a comparability note only when a source states
  one or a reviewer records one (cited); every other pair is
  `comparability_unknown`.

## Data minimisation decision

All three implemented sources publish aggregate statistics; none returns data
about a person or a household. Decision:

- **Stored:** published aggregates per place, period and series key, with
  flags, method statements, definitions and citations.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** HFCS, PHF and SCF microdata, implicates and survey weights,
  rich lists and named individuals or families, household-level records,
  confidential cells (stored as their published status, never as a value). A
  record carrying a person- or household-level field is refused at write time,
  as is a derived, filled, blended, re-harmonised or forecast value.
- **Retention:** release vintages are kept for provenance; no erasure workflow
  applies.

## Bounded first coverage

| Source | Places | Series | Periods | Caps |
| --- | --- | --- | --- | --- |
| WID | Germany (DE), France (FR), United States (US) | `shweal` top 1%, 0.1% and 0.01% shares (`992_j`); `wwealn` and `wwealp` ratios (`999_i`) | from a declared start year | 1 document, 40 series |
| ECB DWA | Germany (DE), France (FR) | net wealth share of `D10` and `B50`; median net wealth | from a declared start quarter | 1 document, 20 series |
| Fed DFA | United States (US) | net worth share of `TopPt1`, `RemainingTop1`, `Next9`, `Next40`, `Bottom50` | from a declared start quarter | 1 document, 20 series |

Justification: Germany and France appear in WID and DWA, the United States in
WID and DFA, so every implemented source has a place it shares with another
and the side-by-side answer is visible without blending. Numeric values reuse
the Economics series storage (`register_series` in
`src/domains/economic/model.py`, domain `society`) as the income and social
protection tracks do; no new series record shape is introduced. Every further
place or variable is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| WID | `unverified-live` | - | none; offline authored fixtures only |
| ECB DWA | `unverified-live` | - | none; offline authored fixtures only |
| Fed DFA | `unverified-live` | - | none; offline authored fixtures only |
| ECB HFCS statistical tables | `unverified-live` | - | none; offline authored PDF fixtures only (the publisher's host did not resolve from the build environment) |
| Bundesbank PHF aggregates | `unverified-live` | - | none; offline authored PDF fixtures only (the publisher's host did not resolve from the build environment) |

The fixtures are authored, not captured: synthetic values for reference
periods 2093-2098 and release dates in 2098-2099, so nothing can be mistaken
for a published figure.
