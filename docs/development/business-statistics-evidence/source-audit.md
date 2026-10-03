# Industry and business statistics: source-contract audit and bounded coverage (IB01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source; the acquisition, store, link and live-validation
issues then cite this document.

This audit sets out, per source, what an `economics.business` provider in the
existing Economics bundle (`packs/economics/`) may acquire, how, and on what
terms. **It was written without network access: the publisher hosts
(`ec.europa.eu`, `api.census.gov`, `www.census.gov`) are blocked by this
runtime's egress proxy (verified 2026-10-03), so terms, endpoints, dataset codes,
variable names and rate limits were not re-verified live.** They come from the
publishers' documentation as the author knows it. Every item marked _verify_ must
be checked against the live pages, the live terms and a real response before the
first dated live run (the track's "Validate live coverage" issue, not yet
opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does not exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/business_statistics_sources.py`
by the track's acquisition issues and must match this audit; the source-pack
entries those issues declare carry the same `live_verification` status.

Non-goals for every source: no nowcasting, no filled periods, no re-basing of
indices, no seasonal adjustment of our own, no blending of Eurostat and Census
figures, no reconstruction of suppressed or noise-infused cells, no rates,
shares or per-establishment figures of our own, no derived indicators, no
forecasts.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `eurostat-sts` | Eurostat, short-term business statistics (European Business Statistics Regulation (EU) 2019/2152, _verify_) | monthly production and turnover indices for industry, construction, trade and services by NACE Rev.2 | Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector (ESTAT path); `sts_inpr_m` (industrial production), `sts_intv_m` (industry turnover) (_verify_ codes and dimension order) | `unverified-live` |
| `eurostat-business-demography` | Eurostat, business demography | active enterprises, births, deaths and survivals, with Eurostat-published rates, by NACE Rev.2 and size class | same path; `bd_9bd_sz_cl_r2` or its EBS successor (the dataset codes changed with the EBS Regulation, _verify_ the current code) | `unverified-live` |
| `us-census-cbp` | US Census Bureau, County Business Patterns | establishments, mid-March employment, first-quarter and annual payroll by NAICS, for nation, state and county | Census Data API, JSON array of arrays, `/data/{year}/cbp` (_verify_ path, variables and NAICS vintage per year) | `unverified-live` |

No source was found whose terms forbid the intended use (store aggregates with
citations, show them side by side, export cited evidence bundles). Should the
live check find otherwise, the source moves to `blocked` in `LIVE_VERIFICATION`.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Eurostat STS | `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=...` | none | Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (_verify_) | no published quota (_verify_); one request per declared document | `LAST UPDATE` dates each release; every monthly release may revise earlier months and each changed release is a new vintage; a new index base year (`unit` `I21` replacing `I15`, _verify_) is a different series, never re-based by us; `OBS_FLAG` (`p`, `e`, `b`, `c`, _verify_) kept per value |
| Eurostat business demography | same endpoint | none | as above | as above | annual, published about two years after the reference year (_verify_); deaths are confirmed only after two years without reactivation, so the latest years' deaths are provisional and are revised in later releases, each a new vintage; flags as above |
| Census CBP | `https://api.census.gov/data/{year}/cbp?get=NAME,ESTAB,EMP,EMP_N,PAYANN,PAYANN_N&for=state:06&NAICS2017=31-33&key=...` (_verify_ variable names, the noise and flag variables and the NAICS variable for each year) | optional API key, optional secret `NOESIS_CENSUS_API_KEY` resolved by the source-pack runtime's secret resolver, sent as `key` and never recorded in evidence, URLs or receipts; without it a lower daily quota applies (_verify_) | Census data are US government works (public domain, _verify_); the Data API terms of service ask applications to state that they use the Census Bureau Data API without endorsement (_verify_ the wording) | about 500 queries per IP per day without a key (_verify_); one request per declared document | one annual release per reference year, dated by the declared release date, else the retrieval time (labelled `retrieval_time`); a later correction of a published year is a new vintage; a NAICS revision (2017 to 2022, _verify_ the first year) is a different classification key, linked only through Census's published concordance |

**CBP disclosure protection.** Since reference year 2017 CBP protects employment
and payroll by noise infusion and publishes a noise flag per value (`EMP_N`,
`PAYANN_N`: low, moderate or high noise, _verify_ letters), and withholds cells
with too few establishments (_verify_ the published rule and the marker). Earlier
years used cell suppression with employment-size range flags (`EMP_F`, _verify_).
Every flag is stored verbatim; a suppressed or withheld cell keeps its status and
is never a zero; a flagged value is never treated as exact; no value is
reconstructed by subtracting published cells from a total.

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, a response larger than the budget, a missing optional
key with the keyless quota exhausted) fails that source's run with its code and a
receipt; earlier vintages stay current and nothing is marked removed or revised
because of a failure. Readiness reports the source as `stale`.

## Definitions recorded per series

- **STS:** the indicator (`indic_bt`), NACE Rev.2 aggregate (`B-D`, `C`), unit
  and index base year, seasonal and calendar adjustment (`s_adj`: `NSA`, `CA`,
  `SCA`) as published; adjusted and unadjusted series are different series.
- **Business demography:** the Eurostat-OECD business demography definitions of
  active enterprise, birth, death and survival; employer or all enterprises;
  size class; Eurostat-published birth and death rates stored as published.
- **CBP:** establishment (not enterprise) as the unit; employment in the pay
  period including March 12; payroll in thousands of dollars (_verify_); the
  NAICS vintage; CBP's scope exclusions (self-employed, private households,
  government, most agriculture, _verify_) as a definition note.
- **Comparability:** Eurostat enterprises and Census establishments are different
  statistical units; NACE and NAICS are mapped only through published concordances
  (the concordances the labour track's LB07 has an operator import), each mapping marked exact, partial
  or one-to-many. CBP employment and BLS or Eurostat employment series in
  `economics.labour` are different series, linked by place and citation only.

## Data minimisation decision

The sources publish aggregate statistics about establishments and enterprises;
none returns data about a person. Decision:

- **Stored:** published aggregates per place, period and series key, with flags,
  noise and suppression markers, notes, definitions and citations.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** business-register and survey microdata, confidential and
  suppressed cells (stored as their status only), the ZIP-code Business Patterns
  and Nonemployer Statistics (not audited here), and any attempt to infer a
  single business's figures. A record carrying a firm- or person-level field, or a
  derived, filled, blended or forecast value, is refused at write time.
- **Retention:** release vintages are kept for provenance; no erasure workflow
  applies.

## Bounded first coverage

| Source | Places | Series | Periods | Caps |
| --- | --- | --- | --- | --- |
| Eurostat STS | Germany (DE) | `sts_inpr_m`: production, `B-D` and `C`, `SCA` and `NSA`, current base year | at most 36 months from a declared start period | 1 document, 10 series per response |
| Eurostat business demography | Germany (DE) | active enterprises, births and deaths, total business economy, all size classes | from a declared start year | 1 document, 20 series |
| Census CBP | California (state `06`) | `ESTAB`, `EMP`, `PAYANN` with their flags for NAICS `00` (total) and `31-33` (manufacturing) | the two most recent reference years | 2 documents (one per year), 50 rows per response |

Justification: Germany and California are the places the labour track already
covers, so Labour links (`economics.labour`) and the labour track's operator
concordance import (LB07) apply unchanged; manufacturing appears in all three
sources, and `economics.trade` flows for the same place are linked by place and
citation only (product and activity classifications are not mapped here); two
CBP years show a release vintage and the noise flags; STS shows adjusted versus
unadjusted and monthly revision. Numeric values reuse the Economics series storage
(`register_series` in `src/domains/economic/model.py`: `economic_vintages`,
`dataset_observations`) as `src/kb/labour_statistics.py` does; CBP needs its own
small JSON reader, following the labour track's BLS reader for key handling. No
new series store or record shape (`statistical-series`) is introduced. Every
further place, year or dataset is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Eurostat STS | `unverified-live` | - | none yet; offline fixtures to be authored |
| Eurostat business demography | `unverified-live` | - | none yet; offline fixtures to be authored |
| Census CBP | `unverified-live` | - | none yet; offline fixtures to be authored |

The fixtures will be authored, not captured: synthetic values for reference
years 2094-2097 and release dates in 2098-2099, so nothing can be mistaken for a
published figure.
