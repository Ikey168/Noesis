# Trade flows: source audit and bounded provider coverage (TF01)

Tracking: #2210 · delivery issue #2535 · recorded 2026-09-29.

This audit sets out, per source, what the Economics bundle's optional trade
features (`trade-comtrade`, `trade-comext`) may acquire, how, and on what terms.
It was written without network access. Endpoints, parameters, field names and
terms come from the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live documentation,
the live terms and a real response before the first dated live run (TF12,
#2556). No provider is `live` until that run exists.** The machine-readable
copy of these decisions is `PROVIDER_CONTRACTS`, `LIVE_VERIFICATION` and
`BOUNDED_COVERAGE` in `src/ingestion/trade_sources.py`; the MCP tool
`trade_source_contracts` returns them, and each source entry in
`config/source_packs/economic.json` carries its `trade_flows.live_verification`
status.

These non-goals apply to every source: flows are stored as each reporter
published them. A reporter's figure and its partner's mirror figure are two
observations, shown side by side with their difference as a displayed
asymmetry and never reconciled into one value. Missing, confidential or
suppressed flows are never estimated or zero-filled, nothing is nowcast, flows
are never re-allocated between areas, concordances are used only for display
and cited conversion (never with invented weights), and nothing infers
sanctions evasion or makes a compliance determination.

## Access decisions

| Source | Delivers | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- |
| UN Comtrade API (comtradeapi.un.org) | reported goods flows by reporter, partner, flow and HS/SITC commodity | `unverified-live` | Documented REST API with a free subscription key. Endpoint paths (`/data/v1/get/{type}/{freq}/{cl}`, `/data/v1/getDA/...`), the key header (`Ocp-Apim-Subscription-Key`), field names and free-tier limits are _verify_ |
| Eurostat Comext (ec.europa.eu, Comext dissemination API) | EU member-state goods flows by partner, CN8 product, flow and month | `unverified-live` | Anonymous dissemination API reached through the existing Eurostat connector's Comext path. The dataset code `DS-045409`, repeated-parameter selection, the `time` filter, indicator codes and the confidential status code are _verify_ |
| WITS concordances (wits.worldbank.org) | HS edition and HS-SITC concordance files (zip with one CSV) | `unverified-live` | Public files without authentication. File paths, member names, column headers and encoding are _verify_ |
| UN Statistics Division correlation tables (unstats.un.org) | HS edition correlation tables and HS-SITC correspondence tables | `operator-import` | Published as XLSX workbooks; the runtime does not parse XLSX (`openpyxl` is only an optional extra), so an operator records a table's rows with the workbook digest, sheet and citation through `TradeFlowStore.import_concordance`; the relationship column is stored as published |

## Per-source contract

| Source | Endpoints | Key handling and free-tier limits | Licence and redistribution | Releases, revisions and classification vintages |
| --- | --- | --- | --- | --- |
| UN Comtrade | `GET https://comtradeapi.un.org/data/v1/get/C/{A,M}/HS?reporterCode&partnerCode&flowCode&cmdCode&period&includeDesc=true` and the data-availability call `GET .../getDA/C/{A,M}/HS?reporterCode&period` (_verify_) | required secret `NOESIS_COMTRADE_KEY`, sent as the `Ocp-Apim-Subscription-Key` header and never stored in request metadata; free tier about 500 calls a day and up to 100 000 records a call (_verify_); a run is bounded by `max_pages` (two calls per declared document) and `max_results`, and a response over the budget is refused rather than truncated; HTTP 429 is reported as `rate_limited` with `Retry-After` | UN Comtrade terms of use: attribution required; redistribution of extracted data is restricted (_verify_ before redistributing any figure) | each reporter-period is released and revised on its own schedule; the data-availability `lastReleased` stamp dates a release (`firstReleased` is kept); a re-release with changed values is a new vintage and earlier vintages stay queryable; `classificationCode` H0-H6 (HS1992-HS2022) and S1-S4 (SITC revisions) is kept per value with `isOriginalClassification` |
| Eurostat Comext | `GET https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409?format=JSON&reporter&partner&product&flow&indicators&freq&time` (_verify_) | no key; no published per-user limit (_verify_); a request is bounded by its declared categories and a 2 000-cell ceiling | Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required | monthly releases revise earlier months; the cube's `updated` stamp dates a release; the CN is revised every year, so the CN vintage is the period's year (`CN2099`); CN8 codes subdivide HS6 |
| WITS concordances | `GET https://wits.worldbank.org/data/public/concordance/Concordance_H6_to_H5.zip` and `..._H6_to_S4.zip` (_verify_) | no key; one download per declared table | WITS terms; concordances derive from UNSD tables (_verify_ attribution wording) | a table is dated by the declared publication date and identified by its digest; a changed table is a new concordance revision; the file states code pairs, so cardinality (1:1, 1:n, n:1, n:n) is derived from the file's own pairs and labelled `derived-from-published-pairs`; no weight is invented |
| UNSD correlation tables | workbook downloads under `https://unstats.un.org/unsd/classifications/Econ` (_verify_) | operator import; no automated download | UN terms of use (_verify_) | the relationship column as published; weights only when published |

## Bounded coverage (selected)

The first coverage is deliberately small so that every figure can be reviewed
by hand during the live check (TF12):

| Provider | Pairs | Products | Periods | Record caps |
| --- | --- | --- | --- | --- |
| UN Comtrade | Germany (M49 276) and China (156), each as the pair reporter and as the other's mirror | HS chapter 29 (`293090`, organo-sulphur compounds, which the dual-use correlation fixture cites) and chapter 85 (`854143`, photovoltaic modules; the mirror's HS2017 `854140` is requested too) | annual, the two most recent reference years; imports and exports (`M`, `X`) | 50 series per call, 4 calls per run |
| Eurostat Comext | Germany and France (intra-EU), each as reporter and as the other's mirror | CN8 `29309098` and `85414300` | monthly, two declared months per document | 50 series per cube, 2 000 cells per cube |
| WITS | - | HS 2022 to HS 2017 and HS 2022 to SITC Rev.4 tables | the published editions | 1 000 rows per table |

Justification: the pairs exercise both a non-EU pair (reporter and mirror from
two different national systems, CIF against FOB) and an intra-EU pair (the same
customs union, both reported through Comext). The two products connect to the
Sanctions correlation table (`293090` / `29309098` for control code 1C350) and
to an HS 2022 split (`854140` into `854141`-`854149`), so cross-vintage filtering
and non-exact mappings are exercised. The source-pack fixtures are authored in
the documented response shapes with fictional values
(`tests/fixtures/source_packs/economic-trade-*.json`,
`tests/fixtures/trade/`).

## Gap against the existing Eurostat connector and Economics stores

* `src/ingestion/connectors/dataset/eurostat.py` already had an `api: comext`
  path (added for the Sanctions trade context, `src/kb/sanctions_trade.py`), but
  its `parse` extracts one series per cube by collapsing every multi-category
  dimension to its first category and cannot repeat a query parameter. The
  connector is extended, not duplicated: list-valued filters repeat the
  parameter (scalar filters encode exactly as before), `comext_url` names the
  Comext path, and `parse_cells` reads every cell of the cube with its status
  flag, never zero-fills an absent cell and keeps the `updated` stamp.
* The generic Economics series store (`economic_vintages` in
  `src/domains/economic/model.py`) keys a series by one geography and has no
  place for a partner, a product code in a classification vintage, a customs
  basis or a reporter-versus-mirror role; the Comext flows the Sanctions context
  registers there are single-geography series. Trade flows therefore follow the
  Economics series pattern of `src/kb/demographics.py` (release, series,
  appended vintage, observation, release and retrieval clocks with basis
  labels, `revision_of`) in their own namespace-scoped `trade_*` tables in
  `src/kb/trade_flows.py`, with comparability notes following
  `src/kb/demographics_comparability.py`.
* No store held classification concordances; they are new records in the same
  module (concordance revisions, rows with mapping type and basis, labels per
  vintage).

## Source-pack entries

`config/source_packs/economic.json` (`economic-statistics-and-filings` 1.5.0)
declares `un-comtrade-trade-flows`, `eurostat-comext-trade-flows` and
`wits-classification-concordances` (connector `trade-flows`, mapping
`noesis-trade-flow-record-v1`), each with `trade_flows.live_verification:
unverified-live` and a pinned authored fixture that replays through the real
adapter offline. Earlier sources are unchanged.

**Live evidence.** Adapter receipts and stored releases state
`evidence_origin: fixture` for fixture transports and `live` otherwise. Nothing
is reported as live coverage until a dated run is recorded in
[`README.md`](README.md).
