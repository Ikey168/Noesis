# Economics trade-flows guide

The Economics pack's optional trade features (#2210) answer one question: for
a country pair, a product and a period, what did each side report, as of a
release? `trade-comtrade` covers UN Comtrade and `trade-comext` covers
Eurostat Comext. Both are off by default and independent of each other.

Each flow is returned as published. The reporter's own figure and the
partner's mirror figure (the partner's report of the opposite flow) are shown
side by side. Each figure carries its valuation basis (CIF or FOB), its
classification vintage (HS edition, CN year or SITC revision) and the release
it came from. The difference between the two figures is shown as an
asymmetry. It is never reconciled into one value.

Source contracts, licences, rate limits and the bounded first coverage are in
the [source audit](../development/trade-evidence/source-audit.md). No provider
is live until a dated run verifies it
([evidence](../development/trade-evidence/README.md)).

## Enable it

Select one or both features in the Economics bundle composition, for example
`features: ["trade-comtrade", "trade-comext"]`. Either one binds
`economics.trade`, `geospatial.core`, `platform.subscriptions` and
`platform.source-runtime`.

Sanctions and Ownership are not required. When their stores are absent, links
and the sanctions variant report `provider_absent`.

The `economics.trade-flows` profile carries the vocabulary (reporter figure,
mirror figure, asymmetry, CIF, FOB, classification vintage) and the query
defaults. `trade_readiness` reports which features are selected, the release
count per provider and each provider's live-verification status.

## Acquire

Run these `economic-statistics-and-filings` (1.5.0) sources through the
source-pack tools:

- `un-comtrade-trade-flows` needs `NOESIS_COMTRADE_KEY`. Each declared
  document is one reporter's report, either the pair's own report or the
  partner's mirror report. The data-availability `lastReleased` stamp dates
  the release.
- `eurostat-comext-trade-flows` goes through the Eurostat connector's Comext
  path. The cube's `updated` stamp dates the release. Confidential cells keep
  their flag and have no value.
- `wits-classification-concordances` loads the HS 2022 to HS 2017 and HS 2022
  to SITC Rev.4 tables. Cardinality is derived from each file's own code
  pairs, and weights are never invented.

UNSD correlation workbooks are XLSX files. Record them with
`import_trade_concordance`, giving the URL, the publication date, the file
digest and each row's relationship as published.

To refresh on demand, use `TradeMonitor.refresh`. It stays within the source's
page budget, writes a receipt for each run, stops at the first rate-limit
answer, and does not contact the provider again before the provider's
`Retry-After` has passed.

## Ask

| Question | Tool |
| --- | --- |
| Which series exist for a pair, product, flow or role? | `list_trade_series` |
| One series' values in one vintage, or as of a date | `trade_series_values` |
| A pair's flows as of a release, reporter and mirror side by side | `query_trade_flows` (each figure cites its release; a pair with no figure is `none_reported`) |
| Reporter against mirror for one product and direction | `compare_trade_mirror` (the asymmetry is displayed; it is withheld for confidential cells, several figures or different units) |
| A product in another classification vintage | `map_trade_product_code`, `list_trade_concordances` (1:n, n:1 and n:n are flagged non-exact) |
| Which areas and product codes resolve to what? | `propose_trade_area_matches`, `propose_trade_product_matches`, `review_trade_identity_match`, `revert_trade_identity_match`, `list_trade_identity_assertions` (special areas stay distinct) |
| Flows in products a sanctions measure covers | `link_trade_sanctions`, then `query_sanctioned_trade_flows` (lookup aid; covered products without a flow are `none_reported`) |
| An ownership record a source names in connection with a flow | `link_trade_ownership` (source and locator required), `list_trade_links` |
| How comparable are two series? | `record_trade_comparability`, `review_trade_comparability`, `list_trade_comparability_notes` |
| Everything cited for a report | `export_trade_evidence_bundle` (every figure with its source, classification vintage, release vintage and as-of time) |
| Tell me when a release publishes or revises flows for a pair | `create_trade_monitor`, `run_trade_monitor`, `poll_trade_monitor` |

## What it never does

- It never estimates, imputes or zero-fills a missing, confidential or
  suppressed flow.
- It never nowcasts.
- It never reconciles, averages or corrects reporter and mirror figures.
- It never re-allocates flows between areas, and it never merges special
  areas (areas n.e.s., former countries, customs unions) with a country.
- It never converts flows between classifications with invented weights.
  Concordances are used only for display and for cited matching.
- It never infers sanctions evasion or makes a compliance determination. A
  sanctions link only states that a measure cites the flow's product code.
