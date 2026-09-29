# Economics demographics guide

The Economics pack's optional `demographics` feature (default off, #1914)
assembles population, migration, asylum and displacement series for a
geography and a period. Each series comes with its definitions, units,
geography level and vintages. Series from different publishers stay
separate, with explicit comparability notes. Regional series are projected
onto the Geospatial boundaries the platform already holds. Series are linked
by citation to the acts, court decisions and legislative dossiers that Legal
and Political hold.

Source coverage and access decisions are in the
[source audit](../roadmaps/economics-demographics-source-audit.md). No
provider is live until a dated run verifies it.

## Enable it

Select the feature in the Economics bundle composition (`features: ["demographics"]`).
It resolves on its own or together with `public-finance`, and it binds these
providers: `legal.core`, `political.core`, `geospatial.core`,
`platform.subscriptions` and `platform.source-runtime`. The
`economics.migration` profile carries the vocabulary (stock, flow,
citizenship, country of birth, application, decision) and the query defaults.
`demographic_readiness` reports whether the feature is selected, and gives
each provider's access decision and release count.

## Acquire

Run the `economic-statistics-and-filings` (1.3.0) sources through the
source-pack tools:

- `eurostat-demography-migration`: Eurostat JSON-stat with flags and the dataset `updated` vintage.
- `unhcr-refugee-data-finder`
- `iom-dtm-idps`: needs `NOESIS_IOM_DTM_KEY`.
- `destatis-genesis-population-migration`: needs `NOESIS_DESTATIS_GENESIS_TOKEN`.
- `statistik-bb-bezirke`

BAMF publishes PDF reports only. Record their figures with
`import_demographic_figure_sheet`: report URL, publication date, definitions
quoting the report, and page and table locators. They are never scraped.

Declare publisher references (CELEX, ELI, ECLI, BGBl, printed paper, EU
procedure) per document from the dataset metadata page. Nothing is inferred
from a title.

## Ask

| Question | Tool |
| --- | --- |
| Which series exist for a concept, publisher, geography or level? | `list_demographic_series` |
| A series' vintages, definition history, breaks and citations | `inspect_demographic_series` |
| Values in one vintage, or as of a release date | `demographic_series_values` (`historical_vintage_unavailable` when none was retained) |
| Each publisher side by side for a concept, geography and period | `compare_demographic_publishers` (unnoted pairs are `comparability_unknown`) |
| Persons in thousand persons | `convert_demographic_units` (receipt, never stored, never count to rate) |
| Series for a boundary | `resolve_demographic_geographies`, then `query_demographic_boundary` (other levels are listed, never apportioned; replay with `replay_demographic_query`, keep with `pin_demographic_query`) |
| Which acts, decisions and dossiers does a series cite, and the reverse | `link_demographic_references`, `link_demographic_dossier`, `list_demographic_links`, `demographic_series_citing` |
| Tell me about new releases, revisions, definition changes and breaks | `create_demographic_monitor`, `run_demographic_monitor`, `poll_demographic_monitor` |

## What it never does

- It never projects a population.
- It never claims a cause of migration or a policy effect. A citation link is
  only a citation.
- It never merges, averages, nets, chains across a break, rebases or
  apportions values.
- It never treats a keyword overlap as a link. Such an overlap is a discovery
  candidate until a reviewer accepts it.
