# Economics: tourism statistics

The Economics bundle (`packs/economics/`) gains the provider
`economics.tourism` (track #2739, subdomain `tourism-hospitality`) behind the
optional `tourism-statistics` feature (default off). It answers: *given a place,
an indicator and a date, how many nights spent, arrivals, establishments or bed
places did Eurostat publish, for which residence of guest, accommodation type,
frequency and NUTS version, with which flags, in which release?*

It quotes what the source released. It never nowcasts, fills a month or region
the source did not publish, seasonally adjusts a series of its own, blends
Eurostat and UN Tourism figures, computes an occupancy rate, average, per-capita
or per-bed figure or an annual total from months, derives any other indicator,
or forecasts.

## Sources

Two sources in the `economic-statistics-and-filings` source pack (1.8.0,
`config/source_packs/economic.json`), connector `tourism-statistics`, both
`unverified-live` until the dated live run (TO12, #2811). Access, terms, rate
limits, the revision model, the minimisation decision and the bounded coverage
are in the [source audit](../development/tourism-evidence/source-audit.md); it
was written without network access, so items marked _verify_ were not checked
live.

| Source | Publisher | Bounded first coverage | Access |
| --- | --- | --- | --- |
| `eurostat-tourism-occupancy` | Eurostat tourism statistics (SDMX-CSV through the SDMX connector) | Germany: `tour_occ_nim` (nights spent) and `tour_occ_arm` (arrivals), monthly, total, domestic and foreign residence, `I551-I553`, at most 36 months, 2 documents, 30 series per response; Berlin (`DE30`): `tour_occ_nin2`, annual, total residence, 1 document, 10 series | no key |
| `eurostat-tourism-capacity` | Eurostat tourism statistics (SDMX-CSV) | Berlin (`DE30`): `tour_cap_nuts2` establishments and bed places, annual, 1 document, 10 series; the capacity reference date stated per value | no key |
| UN Tourism statistics | UN Tourism (formerly UNWTO) | none: `not-implemented` (no stable machine access, terms not established); queries say so, never answer empty | - |

A failed document (HTTP error, redirect to another host, schema drift, a
response over the budget) fails that source's run with its code and a receipt;
earlier vintages stay current, nothing is marked revised or removed, and
readiness reports the source as `stale`.

## Records and vintages

`noesis-tourism-statistics-record-v2` (`src/kb/tourism_records.py`,
`src/kb/tourism_store.py`). A **series** is keyed by dataset, indicator,
residence of guest (`c_resid`), accommodation type (`nace_r2`), unit, frequency
and geography with its NUTS version. So a monthly national and an annual NUTS 2
series are always different series, and the same code under NUTS 2021 and NUTS
2024 is two series. Each value keeps its text as published, its status
(`reported`, `confidential`, `not_published`) and its `OBS_FLAG` letters
verbatim (`p`, `e`, `b`, `c`, `d`, ...). A confidential cell (`c`) is a status
and carries no value. Each country's establishment-size threshold is part of
the definition, and a `d` flag is a source-stated comparability note.

Each release that changes a series is an appended **vintage** with release
(`LAST UPDATE`) and retrieval clocks; a release dated after its retrieval is
refused. A provisional month revised later is a new vintage, never an
overwrite. A series a complete later release no longer states gets a
`removed_by_source` vintage. Values also live in the Economics series storage
(`economic_vintages`, `dataset_observations`, domain `economics`).

## Answers

- `tourism_indicator_for_place` - a place (a place id through accepted matches,
  or a place key `{scheme: eurostat-geo, code, nuts_version}`), an optional
  concept, frequency, residence and period, as released by the date: one row
  per series with definition and threshold, flags, confidential cells as their
  status, notes and the cited vintage. Rows are grouped by frequency and never
  combined; an annual period is never answered from months. UN Tourism appears
  under `not_implemented`.
- `tourism_series_history` - every vintage with new, revised and dropped
  periods (values and flags before and after), definition changes and
  removals, and per release pair the source-stated notes: provisional months
  revised, breaks (`b`), definition differences (`d`), threshold changes and
  NUTS version changes through the published correspondence. A pair without
  notes is `comparability_unknown`.
- `export_tourism_evidence_bundle` - the answer as assertions, each citing its
  source, record revision (vintage) and as-of time.

## Identity, links and monitoring

- `propose_tourism_place_matches` offers each place key to Geospatial places by
  published code first (code and version, then code, then the ISO equivalent of
  a NUTS 0 code); names are never a match. A reviewer accepts or rejects
  (`review_tourism_identity_match`), reverts are recorded, and unmatched keys
  stay visible.
- `import_tourism_nuts_correspondence` records Eurostat's published NUTS
  correspondence with its citation; `propose_tourism_nuts_links` turns its rows
  into reviewable links between place keys of different NUTS versions. An
  accepted link lets a NUTS 2024 query reach NUTS 2021 rows, labelled as such;
  series are never merged.
- `link_tourism_series` links each series to the Geospatial boundary feature of
  its code in its NUTS version (`gisco:nuts:<version>`, `NUTS_ID`) and to Labour
  series for NACE Rev.2 section `I` for the same place (shared code or accepted
  match), pinning both revisions; never a ratio. An absent provider is
  `provider_absent`, no held target is `target_not_held`.
- `create_tourism_monitor` / `run_tourism_monitor` notify new releases, new
  periods, revised values, definition changes and removals through
  `platform.subscriptions`, citing the vintages before and after. A failed
  refresh never produces a removal notice.

## Data minimisation

Published aggregates only. Traveller survey microdata (`tour_dem_*`),
establishment-level returns and experimental platform data (`tour_ce_oa*`) are
never acquired; person-, guest- and establishment-level fields and derived,
filled, blended or forecast values are refused at write time. Reads need
`knowledge:tourism:read` and namespace access.

## What is not live

Offline: `tests/unit/domains/test_tourism_*.py`, the acceptance journey
`tests/unit/domains/test_tourism_acceptance.py` and
`tests/unit/composition/test_economics_tourism_composition.py`. The fixtures
are authored with fictional values and reference periods (2094-2097) and
past-dated releases (see the audit's deviation note). Live: none yet (TO12);
every Eurostat source stays `unverified-live`, UN Tourism `not-implemented`,
and offline coverage is not live coverage.
