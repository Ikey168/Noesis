# Clinical Evidence health-system capacity: source-contract audit and bounded coverage (HS01)

Tracking: #2215 · delivery issue #2442 · recorded 2026-09-30.

This audit sets out, per source, what the Clinical Evidence pack's optional
`health_capacity` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, dimension names, codes and terms come from
the providers' published documentation as the author knows it. **Every item
marked _verify_ must be checked against the live service and a published
response before the first dated live run (HS12, #2481). No provider is `live`
until that run exists.** The machine-readable copy of these decisions is
`CAPACITY_CONTRACTS` and `BOUNDED_COVERAGE` in
`src/ingestion/health_capacity_sources.py`; the one new provider (`oecd-health`)
is merged into the clinical `PROVIDER_CONTRACTS` of
`src/ingestion/clinical_providers.py` (MCP tool `clinical_provider_contracts`).
The sources are new entries of the existing
`config/source_packs/clinical-evidence.json` (version 0.1.3, the 0.1.2 sources
unchanged): `who-gho-health-capacity`, `oecd-health-statistics` and
`eurostat-health-care-resources`, all on the `health-capacity` connector (the
surveillance connector restricted to capacity documents).

These non-goals apply to every source:

- No health-system performance ranking, league table or quality score. Answers
  are per place and never order places by an indicator.
- No harmonised, adjusted, re-estimated or combined value. Sources stay side by
  side; where definitions differ a reviewable comparability note says so.
- A definition change is a marked break in the series; earlier values are never
  restated. A missing value is unknown.

## Access decisions

| Source | Delivers | Access (reuse) | Auth | Licence / terms and attribution | Rate limits | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| WHO Global Health Observatory | indicator values per country, WHO region and global with reference year, value date and publish state | **reuses the existing `who-gho-odata` path** of `src/ingestion/surveillance_sources.py` (`gho_urls`, `parse_gho`): `ghoapi.azureedge.net/api/Indicator?$filter=IndicatorCode eq '<code>'` and `ghoapi.azureedge.net/api/<IndicatorCode>[?$filter=SpatialDim eq '<ISO3>']`; no second GHO client. WHO has announced a successor data API; the OData host's continued service is _verify_ | none | WHO data terms of use, most GHO data CC BY-NC-SA 3.0 IGO (_verify_); attribution "World Health Organization, Global Health Observatory, indicator <code>" | undocumented (_verify_); at most 10 pages per indicator per run | `unverified-live` |
| OECD Health Statistics | health care resources (beds), workforce (physicians, nurses) and System of Health Accounts expenditure per country and OECD aggregate | **reuses the existing SDMX connector** (`SDMXConnector('OECD').csv_url` / `parse_csv`, SDMX-CSV `format=csvfile`) wired as the `oecd-sdmx-csv` format in `src/ingestion/surveillance_sources.py` (`parse_oecd`): `sdmx.oecd.org/public/rest/data/<AGENCY,DSD@DATAFLOW,VERSION>/<key>` | none | OECD terms and conditions; OECD data CC BY 4.0 since 2024 (_verify_); attribution "OECD, OECD Health Statistics, dataflow <id> version <v>" | per-IP request limit documented by the OECD API (_verify_, around 20/minute); one request per declared dataflow key per run | `unverified-live` |
| Eurostat health care resources and expenditure | `hlth_rs_*` (beds, personnel) and `hlth_sha11_*` (expenditure by financing scheme, function, provider) per country, NUTS region and EU aggregate | **reuses the existing `eurostat-sdmx-csv` path** (`eurostat_url`, `parse_eurostat` through `SDMXConnector('ESTAT')`) and its `OBS_FLAG` mapping; no second Eurostat client | none | Eurostat reuse policy (Commission Decision 2011/833/EU); attribution "Eurostat, dataset <code>" | fair use; one request per declared dataset key per run | `unverified-live` |

## Bounded indicator list

| Domain | WHO GHO (IndicatorCode) | OECD Health Statistics (dataflow, MEASURE) | Eurostat (dataset, dimension code) |
| --- | --- | --- | --- |
| Hospital beds | `WHS6_102` hospital beds per 10 000 population | `OECD.ELS.HD,DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC,1.0`, `HOSP_BEDS` per 1 000 population (_verify_ dataflow and codes) | `hlth_rs_bds1`, `facility=HBEDT` per 100 000 inhabitants (`P_HTHAB`) |
| Health workforce | `HWF_0001` medical doctors per 10 000; `HWF_0006` nursing and midwifery personnel per 10 000 (_verify_ code) | `OECD.ELS.HD,DSD_HEALTH_EMP_REAC@DF_PHYS,1.0`, `PRACT_PHYS` practising physicians per 1 000 (_verify_) | `hlth_rs_prs2`, `isco08=OC221` medical doctors, `OC2221` nursing professionals (live set; not in the offline fixtures) |
| Expenditure by financing scheme (SHA) | `GHED_CHEGDP_SHA2011` current health expenditure as % of GDP; `GHED_GGHE-DCHE_SHA2011` government share of CHE (_verify_ code) | `OECD.ELS.HD,DSD_SHA@DF_SHA,1.0` by `FINANCING_SCHEME` (_verify_; live set only) | `hlth_sha11_hf`, `icha11_hf=TOT_HF, HF1`, unit `PC_CHE` (% of current health expenditure) |

The bounded set is densities and shares. Per-capita and absolute currency
amounts are out of the initial set: no currency or price conversion is made.

## Definitions and revision behaviour

- **WHO GHO.** The OData API serves the indicator name only; the definition,
  unit and method of estimation are published on the Indicator Metadata Registry
  pages (not machine-readable here). They are declared per document with the
  page locator and a valid-from date and stored as definition revisions carrying
  the release's retrieval time. Values change in place without a release stamp:
  a changed value on re-acquisition is a new vintage (release clock
  `Last-Modified`, or the latest value `Date`, labelled). Rows are keyed by
  `IndicatorCode`, `SpatialDim`, `TimeDim`, `Dim1-3` and `PublishState` where
  the answer states it. A new metadata edition is a new definition revision and
  a marked break from its valid-from date.
- **OECD.** Definitions, sources and methods (including country-specific
  deviations) are published per variable; they are declared per document
  verbatim (`source_note`, `country_notes`) with the definition text per
  `MEASURE`. `OBS_STATUS` is kept on every value (`B` break in series, `D`
  definition differs, `E` estimated, `P` provisional ... ; the code list is
  _verify_). The dataset is updated in place and the `csvfile` answer has no
  update stamp, so the release clock is HTTP `Last-Modified` or the declared
  publication date, labelled. The dataflow **version** is part of the declared
  document and of the native revision, so a version change is a new release and
  a new vintage; an answer naming another dataflow or version is refused.
- **Eurostat.** Metadata in ESMS files (`hlth_res_esms`, `hlth_sha11_esms`),
  with the System of Health Accounts edition (SHA 1.0 before SHA 2011) declared
  per measure code with valid-from and valid-to dates. `LAST UPDATE` is the
  release clock; every update is a new vintage; `OBS_FLAG` letters are kept
  verbatim through the existing mapping (`b` break, `d` definition differs,
  `p` provisional, `e` estimated ...).

## Places and years

| Places | Codes | Handling |
| --- | --- | --- |
| Germany | GHO/OECD `DEU`, Eurostat `DE` | resolved through Geospatial place resolution (`src/kb/geospatial.py`) by the published code only |
| France | GHO/OECD `FRA`, Eurostat `FR` | as above |
| WHO European Region, OECD total, EU27 (2020) | `EUR` (WHO region), `OECD`, `EU27_2020` | aggregates: kept, shown beside countries, **never resolved to or treated as a country** |

Years: 2015-2023 for the live run (_verify_ availability per indicator). The
offline fixtures use the real indicator codes with fictional values for
2096-2098 (see `tests/unit/health_capacity_fixture_builder.py`).
