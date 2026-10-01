# Income, poverty and inequality: source-contract audit and bounded coverage (IP01)

Tracking: #2583 · delivery issue #2588 · recorded 2026-09-30.

This audit sets out, per source, what the Society bundle's `society.income`
provider may acquire, how, and on what terms. **It was written without network
access: the publishers' documentation and terms pages
(`pip.worldbank.org`, `ec.europa.eu`, `www.oecd.org`) were blocked from this
runtime, so terms, endpoints and field names were not re-verified live.** They
come from the tracker's references and the publishers' documentation as the
author knows them. Every item marked _verify_ must be checked against the live
pages, the live terms and a real response before the first dated live run
(IP13, #2648). No source is `live` until that run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and `LIVE_VERIFICATION` in
`src/ingestion/income_distribution_sources.py` and `MINIMISATION` in
`src/kb/income_distribution_records.py`. Each source entry in
`config/source_packs/society.json` (`society-income-distribution` 1.0.0)
states `income_distribution.live_verification: unverified-live`, and the MCP
tool `income_source_contracts` returns the same decisions.

Non-goals for every source: no nowcasting, no filled years, no poverty lines
of our own, no blending of PIP, EU-SILC and OECD figures into one series, no
re-harmonisation of welfare concepts, equivalence scales or income
definitions, and no derived indicators.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `pip-country-estimates`, `pip-regional-aggregates` | World Bank, Poverty and Inequality Platform | country and regional poverty headcounts and gaps at stated lines, Gini, mean and median welfare, decile shares, per PPP round and release | PIP API v1 JSON: `/pip/v1/pip` (country), `/pip/v1/pip-grp` (regional aggregates), `/pip/v1/versions` (release list); each document pins `country`, `povline`, `ppp_version` and `release_version` (_verify_ parameter and field names) | `unverified-live` |
| `eurostat-silc-income-poverty` | Eurostat, EU-SILC | at-risk-of-poverty rate (`ilc_li02`), Gini (`ilc_di12`), at-risk-of-poverty threshold (`ilc_li01`); S80/S20 (`ilc_di11`) and mean/median income (`ilc_di03`) declared as next documents | Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector (ESTAT path) | `unverified-live` |
| `oecd-idd-income-distribution` | OECD, Income Distribution Database | Gini of disposable income, relative poverty rates at 50 % and 60 % of the median, by income definition and methodology | OECD Data Explorer SDMX REST API, `format=csvfile`, through the SDMX connector (OECD path); dataflow `OECD.WISE.INE,DSD_WISE_IDD@DF_IDD` (_verify_ id, version and dimension order) | `unverified-live` |

No source was found whose terms forbid the intended use (store aggregates
with citations, show them side by side, export cited evidence bundles), so no
source is recorded as not implemented. Should the live check find otherwise,
the source moves to `blocked` in `LIVE_VERIFICATION` and the source pack, and
queries report it as unavailable rather than empty.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| PIP | `https://api.worldbank.org/pip/v1/pip?country=DEU&year=all&povline=2.15&ppp_version=2017&release_version=...&fill_gaps=true&format=json`; `/pip-grp?country=ECA&group_by=wb&...` (_verify_) | none; nothing secret is sent or stored | World Bank open data terms, CC BY 4.0 (_verify_); cite PIP and the release version with every value | no published per-user quota (_verify_); one request per declared document, at most 12 documents per source | a `release_version` (`YYYYMMDD_<PPP year>_<PPP revision>_<adaptation>_PROD`) is one vintage dated by its date part; a later release restating past values (new surveys, a PPP revision within the round) is a new vintage with `ppp_revision` recorded; a different PPP round is a different series (the line's PPP base year is in the key); a series a later complete release of the same document no longer states becomes a `removed_by_source` vintage |
| EU-SILC | `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=...` | none | Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement of the source (_verify_) | no published quota (_verify_); large extractions are answered asynchronously, which the declared keys avoid | the SDMX-CSV `LAST UPDATE` column dates each release; the dataflow version (`ESTAT:ILC_LI02(1.0)`) and `OBS_FLAG` letters (`b` break, `p` provisional, `e` estimated, `u` low reliability, `c` confidential) are kept per value; a changed dataset is a new vintage, an unchanged re-publication adds nothing |
| OECD IDD | `https://sdmx.oecd.org/public/rest/data/OECD.WISE.INE,DSD_WISE_IDD@DF_IDD,1.0/{key}?format=csvfile&startPeriod=...` (_verify_) | none | OECD terms; OECD data under CC BY 4.0 since 2024 (_verify_) | about 20 data queries per minute per IP (_verify_) | no update stamp in the response: a changed response is a new release dated by the declared release date, else the retrieval time (labelled `retrieval_time`); `OBS_STATUS` `B` (break) becomes a source-stated comparability note; a methodology change is a different series |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, a response larger than the budget) fails that
source's run with its code and a receipt; earlier vintages stay current and
nothing is marked removed or revised because of a failure. The bundle
readiness reports the source as `stale`.

## Definitions recorded per series

- **PIP:** welfare type per value (`income` or `consumption`, as PIP states it;
  regional aggregates are `mixed`), per-capita welfare, the poverty line and
  its PPP base year, `estimation_type` (`survey`, `interpolation`,
  `extrapolation`) and `survey_year` against the `reporting_year`, survey
  acronym and comparability spell. A change of `survey_comparability` between
  years is a source-stated break.
- **EU-SILC:** the at-risk-of-poverty threshold (60 % of the national median
  equivalised disposable income after social transfers) and the modified OECD
  equivalence scale as definitions; `TIME_PERIOD` is the survey year, and the
  income reference year is kept beside it by the rule Eurostat documents (the
  previous calendar year; Ireland's 12 months before the interview stated as an
  exception, _verify_).
- **OECD IDD:** the income definition (disposable household income) and the
  methodology version (`METHODOLOGY`, e.g. `METH2012`) and definition code
  (`DEFINITION`) in every series key; square-root equivalence scale; the
  declared underlying national survey is a note only. OECD values are never
  mixed with EU-SILC series even where the underlying survey is the same.

## Data minimisation decision

The three sources publish aggregate statistics; none returns data about a
person or a household. Decision:

- **Stored:** published aggregates per place, reference year and series key,
  with flags, notes, definitions and citations.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** EU-SILC user microdata (UDB), PIP survey microdata, LIS and any
  other microdata. A record containing a person- or household-level field
  (`PERSONAL_DATA_FIELDS`) is refused at write time
  (`income_distribution_records.check_item`), as is any key that would carry a
  derived, filled, blended or forecast value.
- **Retention:** release vintages are kept for provenance; nothing is personal,
  so no erasure workflow applies.
- **Who may query:** principals with `knowledge:income:read` and access to the
  namespace; writes need `knowledge:income:write`, reviews
  `knowledge:income:review`.

## Bounded first coverage

| Source | Entities and places | Periods | Caps |
| --- | --- | --- | --- |
| PIP country | Germany (DEU), national reporting level; the $2.15 line (2017 PPP) with `fill_gaps=true` so PIP's own line-up years arrive labelled, and the $3.00 line (2021 PPP) survey years only | every year PIP returns for the pinned release | 2 documents, 400 rows per response |
| PIP regional | Europe and Central Asia (ECA), $2.15 (2017 PPP) | every year of the release | 1 document |
| EU-SILC | Germany (DE): `ilc_li02` (60 % threshold, total population), `ilc_di12`, `ilc_li01` (single person, euro) | from a declared start period | 3 documents, 60 series per response |
| OECD IDD | Germany (DEU): Gini and poverty rate at 50 % of median, current definition, 2012 methodology | from a declared start period | 1 document |

Justification: one country appears in all three sources, which is the
smallest selection that shows the three side by side without blending; the
regional aggregate shows PIP's `mixed` welfare concept and line-up
estimation; two PPP rounds show that a PPP round is never combined. Every
further country or dataset is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| PIP | `unverified-live` | - | offline fixtures only (`tests/fixtures/source_packs/society-pip-*.json`) |
| EU-SILC | `unverified-live` | - | offline fixtures only |
| OECD IDD | `unverified-live` | - | offline fixtures only |

The fixtures are authored, not captured: synthetic values for reference years
2094-2097 and release dates in 2098-2099, so nothing can be mistaken for a
published figure.
