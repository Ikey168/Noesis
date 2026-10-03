# Mortality and health outcomes: source-contract audit and bounded coverage (CD01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

This audit sets out, per source, what the Clinical Evidence bundle's proposed
`clinical.mortality` provider (subdomain `mortality-health-outcomes`) may
acquire, how, and on what terms. No per-track tracker or delivery issue exists
yet; both are opened once this audit names a surviving source, which it does
(see the access decisions). **It was written without network access: the
publishers' hosts (`www.who.int`, `ghoapi.azureedge.net`, `ec.europa.eu`,
`population.un.org`, `ghdx.healthdata.org`) are blocked by this runtime's
egress proxy (CONNECT refused with 403, verified 2026-10-03), so terms,
endpoints, dataset codes and field names were not re-verified live.** They
come from the gap table's candidates and the publishers' documentation as the
author knows it. Every item marked _verify_ must be checked against the live
pages, the live terms and a real response before the track's first dated live
run (its "Validate live coverage" issue). No source is `live` until that run
exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/mortality_sources.py` by
the track's acquisition issues, merged into the clinical `PROVIDER_CONTRACTS`
of `src/ingestion/clinical_providers.py`, and must match this audit; a
difference is resolved by changing this audit first.

Non-goals for every source: no medical or public-health advice, no risk
scores, no forecasts or projections of our own, no age-standardisation, rates,
life-table functions or chapter totals computed by Noesis, no bridge coding
between ICD revisions, no "excess mortality" estimate, and no blending of WHO,
Eurostat, UN and national figures into one series. Values are stored and shown
as each publisher released them, side by side.

## Project decisions (2026-10-03, #2736)

- **WHO licence.** WHO Mortality Database and GHO data are acquired without
  commercial gating: the project treats its use as non-commercial. The licence
  (CC BY-NC-SA 3.0 IGO, _verify_) and its share-alike notice are still recorded
  on every value and every export, so the terms travel with the data.
- **Overlap with `clinical.surveillance`.** Keys that the surveillance provider
  already acquires (Eurostat `hlth_cd_*`, WHO GHO) are read through the
  surveillance capability and never acquired twice. `clinical.mortality`
  declares only new keys.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `clinical-mortality-who-mdb` | WHO Mortality Database | registered deaths by country, year, sex, age group and underlying cause, per ICD revision and tabulation list, with population files | bulk files from the WHO Mortality Database download page: zipped CSV parts per ICD revision (`Morticd10_part1`..`partN`, `Morticd9`, ...) plus `country_codes`, `pop` and a documentation PDF (_verify_ file names, part split and columns) | `unverified-live` |
| `clinical-mortality-who-gho` | WHO, Global Health Observatory | life expectancy and healthy life expectancy (WHO Global Health Estimates), by sex | **reuses the existing `who-gho-odata` path** (`gho_urls`, `parse_gho` in `src/ingestion/surveillance_sources.py`); no second GHO client | `unverified-live` |
| `clinical-mortality-eurostat-cod` | Eurostat, causes of death (`hlth_cd_*`) | deaths by underlying cause (European shortlist) by residence, Eurostat-published crude and standardised death rates | **reuses the existing `eurostat-sdmx-csv` path** (`eurostat_url`, `parse_eurostat` through `SDMXConnector('ESTAT')` in `src/ingestion/connectors/dataset/sdmx.py`) | `unverified-live` |
| `clinical-mortality-un-wpp-files` | UN DESA Population Division, World Population Prospects | abridged period life tables (estimates years only), by sex | bulk CSV download of a named WPP revision (`WPP<rev>_Life_Table_Abridged_Medium_<years>.csv.gz` or similar, _verify_) | `unverified-live` |
| `clinical-mortality-un-wpp-api` | UN DESA, Data Portal API | the same indicators per location and year | `population.un.org/dataportalapi/api/v1/data/indicators/{id}/locations/{loc}/start/{y}/end/{y}` (_verify_); a bearer token issued on request is required (_verify_) | `gated-not-granted` |
| `clinical-mortality-ihme-gbd` | IHME, Global Burden of Disease (GBD Results via GHDx) | modelled deaths, DALYs, YLLs, YLDs by cause, risk, location, age and sex | login-gated interactive Results Tool with downloadable extracts; no documented public API identified (_verify_) | `not-implemented` |

`clinical-mortality-un-wpp-api` is `gated-not-granted`: no token is held and
none was requested; the bulk files cover the bounded coverage without it. If
it is granted later, the token is read from `NOESIS_UN_DATAPORTAL_TOKEN`, sent
only in the request header and never written to a record, receipt or log.

`clinical-mortality-ihme-gbd` is `not-implemented`. GBD data are released
under IHME's free-of-charge non-commercial user agreement accepted by a named,
logged-in user (_verify_ the current terms), which this deployment cannot
accept on an operator's behalf or confirm as non-commercial; downloads come
from a login-gated tool that is never scraped; and GBD values are model
estimates whose redistribution in exported evidence bundles is not clearly
permitted (_verify_). The contract is recorded so answers say GBD is
unavailable rather than empty. Reopening it needs a decision record covering
the agreement holder, the export restriction and an operator-supplied import
path (the ECDC Atlas precedent in `docs/roadmaps/clinical-surveillance-source-audit.md`).

**Overlap with `clinical.surveillance`.** The surveillance provider already
declares `eurostat-health` (`hlth_cd_aro`, `hlth_cd_asdr2`), `who-gho` and
`destatis-health` (GENESIS 23211). ADR-005 keeps existing providers and ids
where they are. The mortality provider therefore declares only **new**
documents on the shared paths; a dataset key the surveillance provider already
declares is read through its capability and cited, never acquired a second
time. Destatis 23211 stays a surveillance source and is linked, not re-declared.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model |
| --- | --- | --- | --- | --- | --- |
| WHO MDB | `https://www.who.int/data/data-collection-tools/who-mortality-database` (download page) and the file URLs it lists (_verify_; a move to a WHO data platform host is possible) | none | WHO terms of use for data; most WHO data CC BY-NC-SA 3.0 IGO (_verify_ for the MDB files); cite "WHO Mortality Database, <file>, <update date>" with every value; licence and share-alike notice carried on every export | none published (_verify_); one download per declared part per run | files are replaced in place as countries submit data; the page's "last updated" date (_verify_) or HTTP `Last-Modified`, labelled, is the release clock; a changed row is a new vintage; a row a later complete file no longer has becomes `removed_by_source`; the tabulation `List` (`07A`..`09N` ICD-7 to ICD-9, `101`, `103`, `104`, `10M` ICD-10, _verify_) is part of every series key, so an ICD revision change is a different series and a marked break |
| WHO GHO | `ghoapi.azureedge.net/api/{IndicatorCode}?$filter=SpatialDim eq '<ISO3>'`; WHO's announced successor API is _verify_ | none | as for the existing `who-gho` contract: CC BY-NC-SA 3.0 IGO for most GHO data (_verify_) | undocumented (_verify_); at most 10 pages per indicator | values change in place; a changed value is a new vintage (existing GHO handling); each GHE round re-estimates the whole series and is kept as the vintage it arrived in |
| Eurostat COD | `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=...` | none | Eurostat reuse policy (Commission Decision 2011/833/EU), reuse with acknowledgement (_verify_) | fair use; large extractions answered asynchronously, which the declared keys avoid (_verify_) | `LAST UPDATE` dates each release; `OBS_FLAG` (`b` break, `p` provisional, `e` estimated, `c` confidential, `u` low reliability) kept per value; a changed dataset is a new vintage |
| UN WPP files | `https://population.un.org/wpp/downloads` and the file URLs it lists (_verify_) | none | CC BY 3.0 IGO (_verify_); cite "UN DESA, Population Division, World Population Prospects <revision>" | none published (_verify_); one file per run, streamed under the byte cap | each revision (2022, 2024, ...) re-estimates every past year: it is a separate release, never merged with another revision; the revision name is in every series key |
| UN WPP API | as above, token header | `NOESIS_UN_DATAPORTAL_TOKEN` (not granted) | as WPP files | _verify_ | as WPP files |
| IHME GBD | `https://vizhub.healthdata.org/gbd-results/` (_verify_) | individual login | non-commercial user agreement (_verify_) | n/a | GBD rounds re-estimate all years; n/a while not implemented |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, an undeclared `List` or age format, a file larger
than the byte cap) fails that source's run with its code and a receipt;
earlier vintages stay current and nothing is marked removed. The provider
reports the source `stale`.

## Definitions and comparability recorded per series

- **Underlying cause of death** as coded under the ICD revision and
  tabulation list the source states. ICD codes align to the version-tagged ICD
  modules of `src/kb/clinical_terms.py` (`icd_module`, `icd_crosswalk_module`);
  an ICD-9 code is never mapped to ICD-10. Known breaks are recorded as
  source-stated notes per country: the switch to ICD-10 (Germany 1998, France
  2000, _verify_), WHO ICD-10 updates (e.g. `U07.1` COVID-19 from 2020), changes
  of coding practice such as automated coding where the source documents them,
  and any future ICD-11 coded submission, which is a new series.
- **WHO MDB:** `Frmat` and `IM_Frmat` age-group formats keep the published
  age bands (`Deaths1`..`Deaths26`, `IM_Deaths1`..`4`, _verify_); deaths by
  country of occurrence or residence as the documentation states (_verify_);
  `Admin1`/`SubDiv` subnational rows are excluded from first coverage.
- **Eurostat:** deaths of residents by the European shortlist (`icd10`
  dimension codes, e.g. `A-R_V-Y` all causes, `C` neoplasms, `I` circulatory,
  _verify_); standardised rates are **Eurostat's** (European Standard
  Population 2013, _verify_), labelled `source-standardised`, never recomputed.
- **WHO GHO and UN WPP:** life expectancy and life-table functions are
  modelled estimates (kind `estimate`/`model-output` as in the surveillance
  contract), not observations; WPP projection variants are not acquired.

Different sources for the same country and year are shown side by side with a
comparability note (residence vs occurrence, tabulation list, estimation
round); nothing is preferred, averaged or reconciled.

## Data minimisation decision

All acquired sources publish aggregates. Decision:

- **Stored:** published aggregates per place, period, sex, age band and cause
  key, with flags, notes, definitions, ICD revision, list and citations.
  Small cells (one or two deaths) are stored as published and never combined
  with other data to describe a person.
- **Excluded:** individual death records, death-certificate data, national
  mortality microdata and multiple-cause record files, Eurostat COD microdata,
  GBD microdata inputs. A record with a person-level field is refused at write
  time by the track's record store.
- **Retention:** vintages are kept for provenance; nothing personal is held,
  so no erasure workflow applies.
- **Who may query:** the existing clinical scopes `knowledge:clinical:read`,
  `:write` and `:review` (`src/kb/clinical_records.py`).

## Bounded first coverage

| Source | Places | Causes and indicators | Periods | Caps |
| --- | --- | --- | --- | --- |
| WHO MDB | Germany and France, national rows only | all causes (`AAA`, _verify_) and the published detailed codes within ICD-10 chapters II (C00-D48) and IX (I00-I99); no Noesis-summed chapter totals | 2015 to the latest year published | the part file(s) holding the two countries, 100 MB per file (_verify_ size), 20,000 stored rows |
| WHO GHO | Germany, France | `WHOSIS_000001` life expectancy at birth, `WHOSIS_000002` HALE at birth (_verify_ codes) | every year returned | 2 indicators, 10 pages each |
| Eurostat COD | DE, FR | `hlth_cd_aro` keys not already declared by `clinical.surveillance` for all causes, `C`, `I`; `hlth_cd_acdr2` crude rates (_verify_ code) | from 2015 | 3 documents, 60 series per response |
| UN WPP files | Germany, France | abridged life tables, both sexes and by sex, estimates years only | 2015 to the last estimates year of the revision | one revision (WPP 2024, _verify_), 1 file, 200 MB streamed (_verify_ size), rows filtered to the two locations |

Justification: two countries with different ICD-10 adoption years and
coding practice show cross-source and cross-revision notes without blending;
two chapters plus all causes exercise list and chapter handling while keeping
the MDB row count small; one WPP revision shows that revisions are separate
releases. If a WPP or MDB file exceeds its cap, that source fails
`budget_exhausted`; it is never truncated. Each further country, chapter or
revision is a source-pack version bump. Links: `clinical.surveillance` series
by shared ICD code and place, `economics.demographics` deaths and population
by place and year, both by explicit citation only.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| WHO MDB | `unverified-live` | - | none yet; fixtures to be authored |
| WHO GHO (mortality indicators) | `unverified-live` | - | none yet |
| Eurostat COD (new keys) | `unverified-live` | - | none yet |
| UN WPP files | `unverified-live` | - | none yet |

`clinical-mortality-un-wpp-api` stays `gated-not-granted` and
`clinical-mortality-ihme-gbd` `not-implemented`; neither has a live check.
Fixtures will be authored, not captured: synthetic values for reference
years 2094-2097 and release dates in 2098-2099, so nothing can be mistaken for
a published figure.
