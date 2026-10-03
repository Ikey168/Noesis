# Economics: industry and business statistics

The Economics bundle (`packs/economics/`) gains the provider
`economics.business` (track #2738, subdomain `industry-business`) behind the
optional `business-statistics` feature (default off). It answers: *given a
place and an indicator, what production indices, enterprise counts, births and
deaths, establishments, employment and payroll did each source publish, for
which statistical unit, classification and vintage, adjustment and base year,
with which flags, in which release, as of a date?*

It quotes what each source released. It never nowcasts, fills a period a
source did not publish, re-bases an index, seasonally adjusts a series of its
own, blends Eurostat and Census figures, reconstructs a suppressed, withheld or
noise-infused cell, or computes a rate, share, per-establishment figure or
other derived indicator, and it makes no forecast.

## Sources

Three sources in the `economic-statistics-and-filings` source pack (1.7.0,
`config/source_packs/economic.json`), connector `business-statistics`, all
`unverified-live` until the dated live run (IB13). Access, terms, rate limits,
the revision model, CBP disclosure protection, the minimisation decision and
the bounded coverage are in the
[source audit](../development/business-statistics-evidence/source-audit.md); it
was written without network access, so items marked _verify_ were not checked
live.

| Source | Publisher | Bounded first coverage | Access |
| --- | --- | --- | --- |
| `eurostat-sts` | Eurostat short-term business statistics (SDMX-CSV through the SDMX connector) | Germany; `sts_inpr_m` production, NACE Rev.2 `B-D` and `C`, `SCA` and `NSA`, current base year; at most 36 months, 10 series | no key |
| `eurostat-business-demography` | Eurostat business demography (SDMX-CSV) | Germany; active enterprises, births and deaths, business economy, all size classes; 20 series | no key |
| `us-census-cbp` | US Census County Business Patterns (Census Data API) | California (state `06`); `ESTAB`, `EMP`, `PAYANN` with noise flags for NAICS `00` and `31-33`; two reference years, one document each | optional `NOESIS_CENSUS_API_KEY`, sent as `key`, never recorded |

## Records and vintages

`noesis-business-statistics-record-v2` (`src/kb/business_statistics_records.py`,
`src/kb/business_statistics_store.py`). A **series** is keyed by source,
dataset, indicator, classification (NACE Rev.2, or NAICS with its vintage),
size class, place, adjustment (`NSA`, `CA`, `SCA` or not applicable), unit with
its index base year, and frequency. So an adjusted and an unadjusted series, a
rebased index (`I21` against a later base) and a series under another NAICS
vintage are always different series. Each value keeps its text as published,
its status (`reported`, `confidential`, `withheld`, `not_published`) and its
flags verbatim: Eurostat `OBS_FLAG` (`p`, `e`, `b`, `c`, ...) and the CBP noise
flags (`EMP_N`, `PAYANN_N`) and withheld markers. A withheld or confidential
cell carries no value (never a zero); a noise-infused value is marked as never
exact.

Each release that changes a series is an appended **vintage** with release
and retrieval clocks: Eurostat's `LAST UPDATE`, the declared CBP release date,
or the retrieval time labelled `retrieval_time`. A release dated after its
retrieval is refused. A series a complete later release no longer states gets
a `removed_by_source` vintage; when the same release states the series under a
new base year, a source-stated `base_year_change` note links the two series,
which stay separate. Values also live in the Economics series storage
(`economic_vintages`, `dataset_observations`, domain `economics`).

## Answers

- `business_indicator_for_place` - each source's figures for a place (a place
  id through accepted matches, or a published area code), optional concept and
  classification code, as released by the date: definition, statistical unit,
  classification and version, size class, adjustment, unit and base year,
  flags, withheld and missing periods, source-stated notes and the cited
  vintage. Pairs of rows list recorded differences and notes, else
  `comparability_unknown`.
- `compare_business_places` - several places side by side, for example Germany
  through Eurostat and California through CBP, with a classification code per
  place. Enterprises and establishments are paired only to state that they are
  different measures.
- `business_series_history` - every vintage with new, revised and dropped
  periods (values and flags before and after), definition changes and removals,
  and per release pair the notes that apply: provisional periods confirmed,
  breaks the source flags, base-year changes with the successor series and
  NAICS vintages as separate classification keys.

## Identity, links and monitoring

- `propose_business_place_matches` matches Eurostat GEO and US FIPS state
  codes to Geospatial places by published code only; a reviewer accepts or
  rejects; reverts are recorded; unmatched codes stay visible.
- `propose_business_classification_links` proposes NACE-NAICS and
  NAICS-vintage *candidate links* through published concordances with their
  citations: this provider's imports (`import_business_concordance`) and,
  read-only, the labour track's LB07 imports. A path through a pivot (NACE `C`
  and NAICS `31-33` both to ISIC Rev.4 `C`) is `partial` unless both legs are
  exact. Accepted links let a classification request reach the other
  classification, labelled as such; series are never merged.
- `link_business_series` links series to Labour series for the same place
  (shared code or both areas accepted as one Geospatial place; the
  classification relation is evidence only), to Trade series reported by the
  place (product and activity classifications are not mapped), and to
  methodology documents by exact URL, pinning both revisions. An absent
  provider is `provider_absent`, no held target is `target_not_held`.
- `create_business_monitor` / `run_business_monitor` notify new releases, new
  periods, revised values, definition changes and removals (naming the
  successor a rebase states) through `platform.subscriptions`, citing the
  vintages before and after.

## Data minimisation

Published aggregates only. Person- and firm-level fields (business-register or
survey microdata, a single business's figures) and derived, filled, blended,
re-based, reconstructed or forecast values are refused at write time. ZIP-code
Business Patterns and Nonemployer Statistics are not acquired. Reads need
`knowledge:business:read` and namespace access.

## Evidence

Offline: `tests/unit/domains/test_business_statistics_*.py`, the acceptance
journey `test_business_statistics_acceptance.py` and
`tests/unit/composition/test_economics_business_composition.py`. The fixtures
are authored with fictional values and reference periods (2094-2097) and
past-dated releases (see the audit's deviation note). Live: none yet (IB13).
Offline coverage is not live coverage.
