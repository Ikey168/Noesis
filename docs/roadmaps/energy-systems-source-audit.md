# Energy Systems: source-contract audit and bounded provider coverage (EN01)

Tracking: #2211 · delivery issue #2231 · recorded 2026-09-29.

This audit sets out, per source, what the Energy Systems pack may acquire,
how, and on what terms. It was written without network access. Endpoints,
parameters, identifiers and terms come from the providers' published
documentation as the author knows it. **Every item marked _verify_ must be
checked against the live documentation, the live terms and a real response
before the first dated live run (EN14, #2271). No provider is `live` until
that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION` in
`src/ingestion/energy_sources.py`; the source pack
`config/source_packs/energy.json` (`energy-systems` 1.0.0) records each
source's access decision beside its bounded selection, and the MCP tool
`energy_source_contracts` returns them.

These non-goals apply to every source: no price forecasting, no dispatch
modelling, no emissions estimation beyond quoting the publisher, no trading
advice. Figures are kept as published per source and release; nothing is
gap-filled, re-aggregated, converted beyond the published unit or blended
across sources. Forecast documents (ENTSO-E day-ahead load forecast, EIA
demand forecast `DF`) are refused as observations.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| ENTSO-E Transparency Platform | generation per type (A75), actual load (A65/A16), day-ahead prices (A44), installed capacity per type (A68) and per production unit (A71), physical cross-border flows (A11) | `acquire` through the **existing** `entsoe` provider in `src/ingestion/environment_providers.py` | Climate and Environment already holds the ENTSO-E client, `securityToken` slot (`NOESIS_ENTSOE_SECURITY_TOKEN`), host policy and XML parsers. The adapter is extended there with the energy documents; the energy pack calls its `plan` and parsers and adds no second ENTSO-E acquisition path, token handling or scheduler |
| US EIA Open Data API v2 | Hourly Electric Grid Monitor (EIA-930) net generation by energy source, demand, BA-to-BA interchange; EIA-860M operating generator capacity | `acquire`, `unverified-live` | Documented public API with a free key (`api_key`, secret ref `NOESIS_EIA_API_KEY`). Route paths, facet names (`respondent`, `fueltype`, `type`, `fromba`, `toba`, `plantid`), capacity column names and the hour-beginning/ending convention are _verify_ |
| Ember | monthly generation by series and demand, yearly installed capacity | `acquire`, `unverified-live` | Ember API with a free key (`api_key`, secret ref `NOESIS_EMBER_API_KEY`); data under CC BY 4.0. Endpoint and parameter names are _verify_. Ember's shares are quoted as `publisher_figures`; emissions and intensity are not acquired |
| Eurostat energy balances (`nrg_bal_c`) | balance items by product (SIEC) and unit per country and year | `acquire`, `unverified-live` through the existing SDMX connector (`SDMXConnector('ESTAT').csv_url` / `parse_csv`) | Anonymous SDMX 2.1 API. Dimension order (`freq.nrg_bal.siec.unit.geo`) and the `OBS_FLAG` column name are _verify_ |
| Energy-Charts (Fraunhofer ISE) | public net power by production type and load | **licence decision: acquire `public_power` only; price endpoints link-only** | Energy-Charts states its data are CC BY 4.0 unless stated otherwise (_verify_ the per-series note). Its public power for European countries re-publishes ENTSO-E data, so records are marked `derived_from: entsoe` and kept apart from ENTSO-E records without reconciliation. Day-ahead prices originate from power exchanges whose redistribution terms Energy-Charts does not grant; ENTSO-E A44 is the price source, so `/price` is link-only |

## Per-source contract

| Source | Authentication | Licence and attribution | Rate limits and pagination | Release and revision behaviour |
| --- | --- | --- | --- | --- |
| ENTSO-E | `securityToken` query parameter from `NOESIS_ENTSOE_SECURITY_TOKEN` (never stored in receipts) | Transparency Platform terms and conditions; attribution "ENTSO-E Transparency Platform" | 400 requests/min per token documented; a selection pins one zone, window and at most 8 documents (≤ 40 requests) | Every document carries `mRID`, `revisionNumber` and `createdDateTime`. Revision 1 is stored as `provisional`, revision > 1 as `revised`; each revision is a new vintage dated by `createdDateTime`. Acknowledgement code 999 is recorded as no data |
| EIA | `api_key` query parameter from `NOESIS_EIA_API_KEY` | U.S. government data, public domain; cite "U.S. Energy Information Administration" | per-key throttling (_verify_ figure); `offset`/`length` ≤ 5000; one page per step, truncation recorded when `total` exceeds the rows returned | EIA-930 values are preliminary and revised by respondents; no per-value release date is published, so a changed value set is a new vintage dated by retrieval time (labelled `retrieval_time`) or by a declared EIA release (EIA-860M monthly release date) |
| Ember | `api_key` query parameter from `NOESIS_EMBER_API_KEY` | CC BY 4.0; attribution "Ember" | not documented as a figure (_verify_); one request per endpoint and country | dataset releases (monthly and yearly) are declared per selection (label and date); a new release is a new vintage beside the prior one; no per-value status (stored `unknown`) |
| Eurostat | none | Eurostat copyright and reuse policy (Decision 2011/833/EU); attribution "Source: Eurostat" | one request per declared series key | `LAST UPDATE` dates the vintage (`provider_last_update`); flags `p` (provisional), `e` (estimated), `b` (break in series) kept verbatim; a value flagged `p` makes the vintage `provisional` |
| Energy-Charts | none | CC BY 4.0 as stated (_verify_); attribution "Energy-Charts, Fraunhofer ISE" | not documented as a figure; one request per country and window | no release or revision marker; a changed value set is a new vintage dated by retrieval time (labelled) |

**Unavailable-access fallback.** A failed step (HTTP error, missing
credential, host outside the declared set, schema drift) is recorded as a
receipt with its failure code; stored vintages stay as they are and the
provider reads as stale in readiness and monitoring. Nothing is inferred.

## Bounded coverage

| Dimension | Selected |
| --- | --- |
| Bidding zones (ENTSO-E) | DE-LU `10Y1001A1001A82H` with borders FR `10YFR-RTE------C` and PL `10YPL-AREA-----S`; FR `10YFR-RTE------C` |
| Countries (Ember, Eurostat, Energy-Charts) | DE (DEU / DE / de), FR (FRA / FR / fr) |
| Balancing areas (EIA) | CISO, ERCO, PJM |
| Plants and units | named explicitly per selection (EIA plant ids, ENTSO-E production-unit mRIDs), at most 20 |
| Datasets | generation by fuel, load, day-ahead price (ENTSO-E only), installed capacity (zone and plant/unit), cross-border flows, energy balances |
| Windows | hourly data at most 7 days per selection; monthly at most 36 months; yearly at most 10 years |

The code table that connects these codes (`config/energy/areas.json`) only
*proposes* reviewable identity matches (EN08); nothing is merged by it.

## Live evidence

Offline fixtures (`tests/fixtures/source_packs/energy-*.json`,
`tests/fixtures/energy/`) are authored in each provider's documented shape
with invented values; they are not live evidence. `LIVE_VERIFICATION` reports
every provider as `unverified-live` until the dated bounded run of EN14.
