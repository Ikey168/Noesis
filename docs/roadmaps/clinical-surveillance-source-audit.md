# Clinical Evidence surveillance: source-contract audit and provider coverage (I01)

Tracking: #1917 · delivery issue #1929 · recorded 2026-09-28.

This audit sets out, per source, what the Clinical Evidence pack's optional
`surveillance` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, layouts, identifiers and terms come from
the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live page and a
published response before the first dated live run (I13, #2034). No provider
is `live` until that run exists.** The machine-readable copy of these decisions
is `PROVIDER_CONTRACTS` in `src/ingestion/surveillance_sources.py`, merged into
the clinical `PROVIDER_CONTRACTS` of `src/ingestion/clinical_providers.py`. The
MCP tools `clinical_provider_contracts` and `surveillance_readiness` return it.

These non-goals apply to every source:

- Values are what the source released, each with its reporting date and its
  reference date as separate fields, its kind (observation, estimate or model
  output), unit, interval, case-definition revision and source revision. A
  value with only one of the two dates says which one is unknown.
- An estimate is never presented as an observation. A value with lower and
  upper bounds is an estimate.
- A case-definition change is a marked break in the series. It is never
  applied to earlier values, and no value is "corrected" across it.
- Sources stay side by side. Nothing is merged, averaged or preferred.
- No outbreak prediction, nowcast, completeness estimate, threshold suggestion
  or health advice is produced. Monitors use thresholds the user sets.
- Portals without documented machine access are never scraped.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| RKI open data (GitHub releases) | notified cases (observations) by district, age group, reporting date and reference date | `unverified-live` | Raw CSV of a pinned release tag or commit on `raw.githubusercontent.com/robert-koch-institut/<repository>/<tag>/<path>`. The tag is the native revision; its release date (GitHub release or Zenodo record) is declared with the document. A new tag is a new vintage; re-running the same tag adds nothing. Repository names, column names (`Meldedatum`, `Refdatum`, `IstErkrankungsbeginn`, `IdLandkreis`, `AnzahlFall`) and the release-date source are _verify_ per repository |
| RKI SurvStat@RKI 2.0 | weekly notified cases by reporting week | `not-implemented` | An interactive application with a manual export. No documented public API or bulk export was identified; the web service the application uses is not documented for reuse (_verify_). It is never scraped. The RKI GitHub releases are the machine-readable path |
| ECDC Surveillance Atlas of Infectious Diseases | reported cases and notification rates per country and year | `not-implemented` (automated) | No documented API was identified (_verify_). The atlas is never fetched or scraped. Operator-supplied CSV exports enter through `import_surveillance_export` in the export's column layout (`HealthTopic`, `Population`, `Indicator`, `Unit`, `Time`, `RegionCode`, `RegionName`, `NumValue`, `TxtValue`; _verify_) with the declared extraction date as the release clock, marked `evidence_origin=operator` |
| WHO Global Health Observatory OData API | observations, estimates with bounds, modelled values | `unverified-live` | `ghoapi.azureedge.net/api`: `/Indicator` for the indicator name, `/{IndicatorCode}` with an equality `$filter` for values, `@odata.nextLink` followed on the same host up to ten pages (a longer answer is refused, never truncated). `TimeDim` is the reference year and `Date` the reporting date (_verify_ the semantics of `Date`). WHO has announced a successor data API; the host's continued service is _verify_ |
| Eurostat health statistics (`hlth_cd_*`) | deaths by cause (observations) per country and NUTS region | `unverified-live` | SDMX 2.1 dissemination API with `format=SDMX-CSV`, read by the existing `SDMXConnector` (a new standard-library SDMX-CSV reader in the same class, so no second SDMX client and no dependency on the optional `sdmx1` package). Same API family as the `eurostat-dissemination` source of `economic-statistics-and-filings`. `LAST UPDATE` is the release clock (its `dd/mm/yy` format is _verify_); `OBS_FLAG` letters are kept per value and `b` marks a break |
| Destatis health statistics (GENESIS-Online, 23211) | deaths by cause per Land (observations) | `unverified-live` (credentialed) | REST API 2020 through the existing `GenesisConnector`: `metadata/table` for the `Updated` stamp and `data/tablefile?format=ffcsv` for values. Credentials (secret `NOESIS_DESTATIS_GENESIS_TOKEN`) go in request headers. The Land-level table code (`23211-0004` in the fixtures), the cause-code Merkmal (`TODUR4` in the fixtures) and the header names are _verify_ |

**Unavailable-access fallback.** A publication is not stored when any of these
happens: an HTTP error, a redirect to another host, a missing credential, an
exhausted budget, schema drift, an undeclared column, unit, dimension, flag or
code system, or a missing release date. The provider's refresh is recorded as
failed in the clinical provider state; the stored vintages stay addressable and
monitors report a `stale-source` event. Nothing is filled in.

## Per-source contract

| Source | Kind of values | Dates | Case definitions | Geography codes | Condition identifiers | Units |
| --- | --- | --- | --- | --- | --- | --- |
| RKI open data | observation | reporting date (`Meldedatum`) and reference date (`Refdatum`); where `IstErkrankungsbeginn = 0` the source substituted the reporting date, so the reference date is recorded as unknown and the source text kept | Falldefinitionen editions (PDF) declared per document with valid-from, valid-to, locator and ICD scope | AGS Kreis codes (`IdLandkreis`); RKI's own Berlin district codes (11001-11012) use the `rki-landkreis` system | disease name as the dataset writes it | cases |
| SurvStat | observation | reporting week only | Referenzdefinition of the edition in force | names, not codes | Meldekategorie names | cases, incidence per 100 000 |
| ECDC Atlas export | observation | reference year or month; no reporting date (recorded as unknown); extraction date is the release clock | EU case definitions, declared per export when known | EU country codes (`EL` for Greece), NUTS, EU/EEA aggregates (`ecdc-aggregate`, no boundary) | health-topic names | `N` (cases), `N/100000` |
| WHO GHO | observation, estimate (bounded values), model output (declared) | `TimeDim` reference year; `Date` reporting date; a row without `Date` has an unknown reporting date | indicator metadata pages, declared when known | ISO 3166-1 alpha-3 (`COUNTRY`), WHO regions and `GLOBAL` (no boundary) | indicator code; a condition label is declared per document | per indicator (rates per 100 000, counts, percent) |
| Eurostat `hlth_cd_*` | observation | reference year; no reporting date | ICD-10 cause groups (the `icd10` code is the condition) | EU country codes and NUTS (version declared per document) | `icd10` dimension codes | `NR` (deaths), `RT` (rate per 100 000) |
| Destatis 23211 | observation | reference year; no reporting date | ICD-10 WHO cause codes | AGS Land codes | ICD-10 cause codes | `Anzahl` (deaths) |

Missing or suppressed markers are absent values in every source: empty, `.`,
`..`, `...`, `x`, `-`, `:`, `/`, `NA`. Their text is kept with a flag, and they
are never compared as numbers. This includes the GENESIS sign `-`, which the
publisher defines as "exactly zero": it is kept as a flag with that meaning,
not as a number.

## Fixtures

Every fetched source in `clinical-evidence` 0.1.1
(`config/source_packs/clinical-evidence.json`) pins an *authored* fixture in
the documented native shape (`tests/fixtures/source_packs/surveillance-*.json`).
The builder is `tests/unit/surveillance_fixture_builder.py`. The ECDC export,
the MeSH and ICD-10 subsets and the reviewer curations are under
`tests/fixtures/clinical/surveillance/`. Repository names, indicator codes,
table codes and every value are fictional (years 2097-2099); geography codes
are real code-list codes. Nothing is a live capture.

## Pack version

The issue asks for the sources in the existing `clinical-evidence` source pack.
They ship as its additive 0.1.1 version: the 0.1.0 sources verbatim plus the
four surveillance sources. The Clinical Evidence bundle keeps its `^0.1.0`
pin, which 0.1.1 satisfies, so the bundle's resolution with the feature off is
unchanged; the `clinical.surveillance` provider pins `^0.1.1`. An installed
0.1.0 upgrades in place.
