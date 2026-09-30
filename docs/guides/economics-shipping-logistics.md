# Economics shipping and logistics guide

The Economics pack's optional `logistics` feature (#2229) answers one
question: for a port, a country or a published route, what maritime and
logistics statistics were published, as of a release? It covers UN/LOCODE port
identities, UNCTADstat maritime series (port calls, container port throughput,
liner shipping connectivity, merchant fleet by flag), Eurostat maritime
transport (goods by port, by country and port to partner port) and the one
freight index cleared as openly licensed (the BLS producer price index for deep
sea freight transportation). The feature is off by default.

Values are stored as published, with their release vintage, definition, unit
and licence. Series of different sources for a similar concept are shown side by
side and never merged. Nothing forecasts freight rates, derives, rebases or
interpolates an index, or infers a route from port totals.

Source contracts, licences, per-index freight decisions and the bounded
coverage are in the [source audit](../roadmaps/economics-logistics-source-audit.md).
No provider is live until a dated run verifies it (#2553).

## Enable it

Select `features: ["logistics"]` in the Economics bundle composition. It binds
`economics.logistics`, `geospatial.core` (`geospatial.feature-query` for port
places), `platform.subscriptions` and `platform.source-runtime`. The sources
ship in the separate source pack `economic-shipping-and-logistics` 1.0.0
(`config/source_packs/economic-logistics.json`); the Economics bundle's
`economic-statistics-and-filings` pin does not change.

## Acquire

Run the four sources through the shared source-pack tools. Each declared
document is one page and one release:

| Source | Records |
| --- | --- |
| `unece-unlocode-ports` | UN/LOCODE port entries of the declared countries for one declared release version |
| `unctadstat-maritime-series` | UNCTADstat report CSVs; a 7z bulk file is refused, and the extracted CSV is recorded with `src.kb.logistics_series.operator_import` (evidence origin `operator`) |
| `eurostat-maritime-transport` | Eurostat `mar_*` cubes through the Eurostat connector |
| `bls-ppi-deep-sea-freight` | the BLS PPI series, its licence on every observation |

Numbers land in the existing Economics series storage (`dataset_series`,
`dataset_observations`, `economic_vintages`); a release that changes no value
adds nothing.

## Match ports

1. `import_logistics_crosswalk` records a published port code list with its
   citation.
2. `propose_logistics_port_matches` makes embedded UN/LOCODEs and crosswalk rows
   exact matches, proposes name matches as candidates and lists unmatched codes.
3. `review_logistics_port_match` accepts or rejects a candidate;
   `revert_logistics_port_match` reverts an accepted one.
4. `project_logistics_port_places` registers ports as Geospatial places, with a
   point only when the release publishes coordinates.

A later UN/LOCODE release that changes or removes a code records a re-match on
every live match; nothing is re-pointed silently.

## Ask

- `query_port_logistics` - a UN/LOCODE (series of every accepted source code) or
  a source code such as `eurostat-port:DE999`, as of a date.
- `query_country_logistics` - country-level series (`m49:276`, `DE`); ports are
  listed, never summed.
- `query_route_logistics` - route-level series only; otherwise `none_on_record`.
- `logistics_value_history` - every vintage of one value.
- `logistics_freight_indices` - in-scope indices and every excluded index with
  its licence reason.

## Join to trade flows

`link_logistics_trade_flows` joins a series to trade-flow series only through a
shared published code (M49, Eurostat GEO, or a port's UN/LOCODE country code).
`link_logistics_economic_series` joins country series to other Economics series
of the same country code, with the Economics comparability check.
`link_logistics_citation` records an explicit citation. Units and frequencies
are listed side by side, never combined. Without trade records the join is
`none_on_record`.

## Monitor

`create_logistics_monitor` watches ports, countries or series. `run_logistics_monitor`
emits `new_vintage`, `revised_value`, `series_break` and `port_code_change`
notices, each with record ids and the source release citation. Unchanged
releases and restarts emit nothing.

## Evidence

`tests/unit/domains/test_logistics_acceptance.py` replays the pinned fixtures
offline with sockets blocked.
