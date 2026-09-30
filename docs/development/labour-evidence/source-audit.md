# Labour statistics: source audit and bounded provider coverage (LB01)

Tracking: #2219 · delivery issue #2448 · recorded 2026-09-29.

This audit sets out, per source, what the Economics bundle's optional
`labour-statistics` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, parameters, field names and terms come from
the providers' published documentation as the author knows it. **Every item
marked _verify_ must be checked against the live documentation, the live terms
and a real response before the first dated live run (LB13, #2493). No provider
is `live` until that run exists.** The machine-readable copy of these decisions
is `PROVIDER_CONTRACTS`, `LIVE_VERIFICATION` and `BOUNDED_COVERAGE` in
`src/ingestion/labour_sources.py`; the MCP tool `labour_source_contracts`
returns them, and each source entry in `config/source_packs/economic.json`
carries its `labour_statistics.live_verification` status.

Non-goals for every source: indicators are stored as each source published
them. Nothing is nowcast or forecast, no missing, suppressed or confidential
period is filled, series from different sources or definition bases are never
blended, averaged or re-harmonised, and no indicator is derived that no source
published.

## Access decisions (`LIVE_VERIFICATION`)

| Source | Access | Decision | Intended status |
| --- | --- | --- | --- |
| ILOSTAT SDMX API (sdmx.ilo.org) | SDMX 2.1 REST, SDMX-CSV (`format=csv`, _verify_), no key | `unverified-live` | `verified-live` after LB13 |
| OECD Data Explorer (sdmx.oecd.org) | SDMX 2.1 REST (.Stat Suite), SDMX-CSV (`format=csvfile`), no key; about 20 data queries a minute per IP (_verify_) | `unverified-live` | `verified-live` after LB13 |
| Eurostat LFS (ec.europa.eu) | SDMX 2.1 dissemination API, SDMX-CSV with `LAST UPDATE` and `OBS_FLAG`, no key | `unverified-live` | `verified-live` after LB13 |
| US BLS Public Data API v2 (api.bls.gov) | JSON, one GET per series ID; registration key `NOESIS_BLS_KEY` sent as `registrationkey` and never recorded; v2 limits 500 queries a day, 50 series a query, 20 years a query (v1 without key: 25/25/10) | `unverified-live` | `verified-live` after LB13 |

All four sources are read through existing code: ILOSTAT, OECD and Eurostat
through `src/ingestion/connectors/dataset/sdmx.py` (`SDMXConnector.parse_csv`;
the ILO provider was added to its SDMX-CSV endpoint table), BLS through the
`labour-statistics` connector's own JSON reader. The key reaches the adapter
through the source-pack runtime's secret resolver
(`src/ingestion/source_pack_runtime.py`).

## Reuse terms

| Source | Terms |
| --- | --- |
| ILOSTAT | ILO copyright; reuse with attribution under the ILOSTAT terms of use (_verify_ the database licence) |
| OECD | OECD terms and conditions: reuse with attribution (_verify_) |
| Eurostat | Commission Decision 2011/833/EU: reuse with attribution |
| BLS | US government work in the public domain; BLS asks for citation (_verify_) |

## Indicators in scope and definition basis

| Source | Employment | Unemployment | Wages / earnings | Hours | Vacancies | Definition basis | Seasonal adjustment |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ILOSTAT | `EMP_TEMP_SEX_ECO_NB`, `EMP_TEMP_SEX_OCU_NB` | `UNE_DEAP_SEX_AGE_RT` (nationally reported), `UNE_2EAP_SEX_AGE_RT` (ILO modelled estimates) | `EAR_4MTH_SEX_ECO_CUR_NB` | `HOW_TEMP_SEX_ECO_NB` | not harmonised (out of scope) | ILO harmonised (19th ICLS Resolution I) for modelled estimates; national definitions as stated in `NOTE_*` for national series | annual series NSA |
| OECD | `DF_IALFS_EMP_WAP_Q` | `DF_IALFS_UNE_M` (harmonised) | `AV_AN_WAGE` | `DF_HOURS` | out of scope | OECD harmonised (ILO guidelines) or national per dataflow metadata | `ADJUSTMENT` dimension: SA, NSA, trend-cycle |
| Eurostat LFS | `lfsa_egan2` (NACE Rev.2), `lfsa_egais` (ISCO-08) | `lfsa_urgan`, `lfst_r_lfu3rt` (NUTS 2) | Structure of Earnings Survey (not in first coverage) | `lfsa_ewhan2` | `jvs_q_nace2` | EU-LFS (Regulation (EU) 2019/1700), flag `d` for national deviations | annual LFS NSA; `s_adj` where published |
| BLS | CES data type 01; CPS `LNS12000000` | CPS `LNS14000000`/`LNU04000000`; LAUS measure 03 | OEWS data type 04; CES data type 03 | CES data type 02 | JOLTS `JO` | national (BLS Handbook of Methods per survey) | series-ID position 3: `S` SA, `U` NSA |

ILO modelled estimates and nationally reported series come from different
dataflows and are different series (their estimate type is part of the series
key); they are never merged.

## Bounded first coverage

| Dimension | Selection |
| --- | --- |
| Places | Germany (ILO/OECD `DEU`, Eurostat `DE`), Berlin (NUTS 2 `DE30`), United States (ILO/OECD `USA`; BLS national surveys), California (LAUS area `ST0600000000000`, FIPS 06) |
| Sectors | manufacturing: ISIC Rev.4 `C` (ILO), NACE Rev.2 `C` (Eurostat), NAICS 2022 `31-33` via CES supersector 30 (BLS) |
| Occupations | ISCO-08 major group 2 (ILO, Eurostat), SOC 2018 `15-1252` (BLS OEWS) |
| Periods | two to three most recent reference periods per declared series |
| Caps | `max_results` series per response, `max_pages` requests per run; a response over the budget is refused, never truncated |

Classifications are mapped only through published concordances (LB07): NACE
Rev.2 to ISIC Rev.4 (Eurostat/UNSD correspondence), NAICS to ISIC (US Census
concordance), ISCO-08 to SOC 2018 (BLS crosswalk), each imported by an operator
with its citation and file digest and each row marked exact, partial or
one-to-many.

## Release and vintage signals

| Source | Signal | How a vintage is dated |
| --- | --- | --- |
| ILOSTAT | per-indicator "last update" on the bulk download page and dataflow annotations (_verify_); ILO Trends/WESO calendar for modelled estimates | declared release date, else the retrieval time of a changed response (labelled `retrieval_time`) |
| OECD | OECD release calendar; dataflow version increments | declared release date, else retrieval time; the stated dataflow version is kept per vintage and a change is a metadata change |
| Eurostat | `LAST UPDATE` column per dataset; Eurostat release calendar | `LAST UPDATE` (`provider_last_update`) |
| BLS | BLS release calendar; `P` (preliminary) footnotes; annual CES benchmark and CPS/LAUS seasonal-factor revisions | declared release date, else retrieval time; preliminary and revised values are distinct vintages; a change beyond the declared regular revision window (`revision_window_periods`, CES: 2 months) or a declared benchmark release is labelled `benchmark_revision` |

Every provider has a `LIVE_VERIFICATION` entry (`unverified-live`, intended
`verified-live` after LB13).
