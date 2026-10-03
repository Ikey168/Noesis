# Social protection: source-contract audit and bounded coverage (SS01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source; the acquisition, store, link and live-validation
issues then cite this document.

This audit sets out, per source, what a `society.social-protection` provider in
the existing Society bundle (`packs/society/`, beside `society.income`) may
acquire, how, and on what terms. **It was written without network access: the
publisher hosts (`ec.europa.eu`, `sdmx.oecd.org`, `sdmx.ilo.org`,
`www.social-protection.org`) are blocked by this runtime's egress proxy
(verified 2026-10-03), so terms, endpoints, dataset codes, dimension names and
rate limits were not re-verified live.** They come from the publishers'
documentation as the author knows it. Every item marked _verify_ must be checked
against the live pages, the live terms and a real response before the first
dated live run (the track's "Validate live coverage" issue, not yet opened). No
source is `live` until that run exists.

The machine-readable copy of these decisions does not exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/social_protection_sources.py`
by the track's acquisition issues and must match this audit; the source-pack
entries those issues declare (beside `config/source_packs/society.json`, pack
id chosen there) carry the same `live_verification` status.

Non-goals for every source: no nowcasting, no filled years, no blending of
ESSPROS, SOCX and ILO figures into one series, no re-classification of one
publisher's functions or branches into another's, no per-capita, per-beneficiary
or share-of-GDP figure of our own, no derived indicators, no forecasts.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `eurostat-esspros` | Eurostat, European System of integrated Social PROtection Statistics | social-protection expenditure and receipts by function and scheme type; pension beneficiaries | Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector (ESTAT path); datasets `spr_exp_sum`, `spr_exp_func`, `spr_pns_ben` (_verify_ codes and dimension order) | `unverified-live` |
| `oecd-socx` | OECD, Social Expenditure Database (SOCX) | public and mandatory private social expenditure by policy area and type (cash, in kind) | OECD Data Explorer SDMX REST API, `format=csvfile`, through the SDMX connector (OECD path); the aggregate SOCX dataflow (an id of the form `OECD.ELS.SPD,DSD_SOCX_AGG@DF_SOCX_AGG,1.0` is the author's recollection, _verify_ agency, id, version and dimensions) | `unverified-live` |
| `ilo-social-protection-coverage` | ILO, ILOSTAT (SDG indicator 1.3.1, fed by the Social Security Inquiry) | share of the population covered by at least one benefit and by contingency (children, old age, unemployment, disability, ...) | ILOSTAT SDMX REST API, SDMX-CSV through the SDMX connector (ILO path); dataflow `ILO,DF_SDG_0131_SEX_SOC_RT,1.0` (_verify_ id and the contingency dimension) | `unverified-live` |
| World Social Protection Data Dashboards | ILO | the same coverage and expenditure figures as interactive dashboards and report tables | no documented machine API known to the author (_verify_) | `not-implemented`: no stable machine access; the ILOSTAT SDMX route above carries the coverage series, and dashboard pages are never scraped |

No source was found whose terms forbid the intended use (store aggregates with
citations, show them side by side, export cited evidence bundles). Should the
live check find otherwise, the source moves to `blocked` in `LIVE_VERIFICATION`
and queries report it as unavailable rather than empty.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| ESSPROS | `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=...` | none | Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (_verify_) | no published quota (_verify_); one request per declared document | `LAST UPDATE` dates each release; the dataflow version and `OBS_FLAG` letters (`b` break, `p` provisional, `e` estimated, `d` definition differs, `c` confidential, _verify_ the list) are kept per value; a changed dataset is a new vintage, an unchanged re-publication adds nothing; a series a later complete release no longer states becomes a `removed_by_source` vintage |
| OECD SOCX | `https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile&startPeriod=...` (_verify_) | none | OECD terms; OECD data under CC BY 4.0 since 2024 (_verify_) | OECD API limits (IP01 records about 20 data queries a minute, ED01 about 60 an hour; the stricter applies until verified, _verify_) | no update stamp in the response (_verify_): a changed response is a new release dated by the declared release date, else the retrieval time (labelled `retrieval_time`); the most recent years are OECD estimates or projections from national budget data (_verify_ the attribute that marks them) and are kept with that status, never treated as final; `OBS_STATUS` `B` becomes a source-stated break |
| ILOSTAT | `https://sdmx.ilo.org/rest/data/ILO,DF_SDG_0131_SEX_SOC_RT,1.0/{key}?format=csv` (_verify_) | none | ILOSTAT terms of use, reuse with attribution (_verify_ the licence, CC BY 4.0 as the author understands it) | none published (_verify_); one request per declared document | per-indicator update date (_verify_ where the response states it), else the declared release date, else retrieval time; World Social Protection Report editions restate earlier years and are new vintages; `NOTE_SOURCE`, `NOTE_INDICATOR` and `NOTE_CLASSIF` attributes are kept verbatim; ILO regional and global modelled estimates are separate series from country data and are not in first coverage |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, a response larger than the budget) fails that
source's run with its code and a receipt; earlier vintages stay current and
nothing is marked removed or revised because of a failure. Readiness reports the
source as `stale`.

## Definitions recorded per series

- **ESSPROS:** the function (`spfunc`: sickness/health care, disability, old
  age, survivors, family/children, unemployment, housing, social exclusion,
  _verify_ codes), the scheme-type and benefit-type dimensions (means-tested or
  not, cash or in kind), the unit as published (`MIO_EUR`, `PC_GDP`, `EUR_HAB`,
  `PPS_HAB`, _verify_) and the ESSPROS manual edition in force. Eurostat-published
  percentages and per-inhabitant values are stored as published, never recomputed.
- **OECD SOCX:** the policy area (nine SOCX branches including health and active
  labour-market programmes), the source of financing (public, mandatory private,
  voluntary private), type (cash, in kind), gross or net basis and unit. Net
  social expenditure is a separate OECD series, not in first coverage.
- **ILOSTAT:** the contingency, sex, the population denominator ILO states in its
  metadata, the reference year and the note attributes. Coverage is a share of
  a population group as ILO defines it; it is never compared with an expenditure
  series as if it measured the same thing.
- **Comparability:** ESSPROS functions and SOCX branches overlap but differ in
  scope (SOCX counts active labour-market programmes and excludes some transfers
  ESSPROS includes, _verify_ the published reconciliation). The COFOG function
  "social protection" in `economics.public-finance` (`gov_10a_exp`) is
  general-government expenditure on an ESA 2010 basis and is a third, distinct
  concept. They are shown side by side, linked by place and citation, never
  merged.

## Data minimisation decision

All three sources publish aggregate statistics; none returns data about a
person or a household. Decision:

- **Stored:** published aggregates per place, reference year and series key,
  with flags, notes, definitions and citations.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** benefit-recipient registers, administrative or survey microdata,
  Social Security Inquiry questionnaire returns, confidential cells (stored as
  their published status, never as a value). A record carrying a person- or
  household-level field is refused at write time, as is a derived, filled,
  blended or forecast value.
- **Retention:** release vintages are kept for provenance; no erasure workflow
  applies.

## Bounded first coverage

| Source | Places | Series | Periods | Caps |
| --- | --- | --- | --- | --- |
| ESSPROS | Germany (DE), France (FR) | `spr_exp_sum` total expenditure (`MIO_EUR`, `PC_GDP`); `spr_exp_func` old age and sickness/health care; `spr_pns_ben` pension beneficiaries, total | from a declared start period | 3 documents, 60 series per response |
| OECD SOCX | Germany (DEU), France (FRA) | public social expenditure, total and old age, % of GDP | from a declared start period | 1 document, 20 series |
| ILOSTAT | Germany (DEU), France (FRA) | SDG 1.3.1 total and old-age coverage, both sexes | from a declared start period | 1 document, 20 series |

Justification: two places present in all three sources are the smallest
selection that shows expenditure (ESSPROS, SOCX) beside coverage (ILO) without
blending; old age appears in every source and shows the scope differences. Links
reuse existing owners: the place through the Geospatial place identity used by
`society.income`, the population denominator through `economics.demographics`
and COFOG social-protection expenditure through `economics.public-finance`, each
by shared published code or accepted match, citing pinned vintages, as
`src/kb/income_distribution_links.py` does. Numeric values reuse the Economics
series storage (`register_series` in `src/domains/economic/model.py`:
`economic_vintages`, `dataset_observations`) as `src/kb/labour_statistics.py`
and `src/kb/demographics.py` do; no new series store or record shape
(`statistical-series`) is introduced. Every further place or dataset is a
source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| ESSPROS | `unverified-live` | - | none yet; offline fixtures to be authored |
| OECD SOCX | `unverified-live` | - | none yet; offline fixtures to be authored |
| ILOSTAT SDG 1.3.1 | `unverified-live` | - | none yet; offline fixtures to be authored |
| World Social Protection Data Dashboards | `not-implemented` | - | no stable machine access (_verify_) |

The fixtures will be authored, not captured: synthetic values for reference
years 2094-2097 and release dates in 2098-2099, so nothing can be mistaken for a
published figure.
