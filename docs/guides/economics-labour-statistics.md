# Economics labour-statistics guide

Given a place, a sector or an occupation, what labour-force indicators
(employment, unemployment, wages, hours, vacancies) have the sources published,
under which definitions, seasonal adjustment and release vintages, as of a date?
The Economics bundle's optional `labour-statistics` feature (#2219) answers
from ILOSTAT, the OECD Data Explorer and Eurostat LFS (all through the SDMX
connector) and the US BLS Public Data API. No new pack or series store exists:
values and vintages live in the Economics series storage
(`economic_indicators`, `economic_series_map`, `economic_vintages`,
`dataset_observations`); `labour_*` tables hold only the labour metadata.

Exclusions: no nowcasting, no labour-market forecasts, no filled periods, no
blending or re-harmonisation of series across sources or definition bases, no
derived indicators.

## Enable and acquire

Select the feature in the composition plan
(`features: ["labour-statistics"]`); it binds `economics.labour`,
`geospatial.core`, `platform.subscriptions` and `platform.source-runtime`.
Acquisition runs through the `economic-statistics-and-filings` source pack
(1.6.0): `ilostat-labour-indicators`, `oecd-labour-statistics`,
`eurostat-lfs-labour` and `bls-public-data-api` (registration key
`NOESIS_BLS_KEY`, never recorded). Every provider is `unverified-live` until a
dated live run ([source audit](../development/labour-evidence/source-audit.md)).

## Records

- **Indicator (series)**: source, native key, concept and measure, unit and
  unit multiplier, NSA/SA/trend, definition basis (ILO harmonised, OECD
  harmonised, EU-LFS, national), estimate type (ILO modelled estimates and
  national series are separate series), place, sector/occupation with
  classification version.
- **Definition**: revisioned, with age bounds, coverage, the source's notes and
  the references it cites.
- **Vintage**: one release that changes a series; records new periods, revised
  values, benchmark revisions and definition/dataflow-version changes. Earlier
  vintages stay queryable; unchanged re-publications add nothing.
- **Observation**: value text as published, status, flags and footnotes.

## Identity, citations and comparability

`propose_labour_place_matches` offers area codes to Geospatial places by the
published code (ambiguous codes wait for a reviewer's choice);
`import_labour_concordance` and `propose_labour_classification_matches` map
NACE/NAICS/ISIC and SOC/ISCO codes only through cited published concordances
(exact, partial, one-to-many). `link_labour_citations` resolves references by
exact URL or CELEX and denominators only when the source names them; the rest
stay unresolved citations. Comparability notes follow the demographics
structure; source-stated breaks are attached to their periods.

## Ask

`labour_indicators_for_place` takes a place id (or a native code), a sector or
an occupation, an optional concept and `as_of_ms`, and returns every source's
vintage current at that date side by side with definition, seasonal adjustment,
flags, gaps and comparability notes; every value cites source, series key,
vintage and retrieval time. `labour_series_history` lists every vintage.
`create_labour_monitor` subscribes to a series or a place/sector/occupation and
notifies new periods, revised values, benchmark revisions and definition
changes.

The offline journey is `tests/unit/domains/test_labour_acceptance.py`.
