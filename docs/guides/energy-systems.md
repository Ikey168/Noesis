# Energy Systems guide

The Energy Systems bundle (`packs/energy`, tracking #2211) takes a bidding zone, country, balancing area or plant
to published electricity generation by fuel, load, day-ahead prices, installed capacity and cross-border flows,
plus energy balances. Every figure keeps its source, release vintage, publication status and as-of time.
Provisional and revised figures are distinct vintages; sources are shown side by side and never blended.

It does **not** forecast prices or load, model dispatch, estimate emissions (Ember's own figures are quoted as
Ember's) or give trading advice. Forecast documents are refused as observations.

## Sources

| Source | What is acquired | Access |
| --- | --- | --- |
| ENTSO-E Transparency Platform | A75 generation per type, A65 actual load, A44 day-ahead prices, A68/A71 installed capacity (zone, production unit), A11 physical flows | the Climate and Environment `entsoe` adapter (no second acquisition path); `NOESIS_ENTSOE_SECURITY_TOKEN` |
| US EIA Open Data API v2 | hourly generation by fuel, demand, interchange; EIA-860M plant/generator capacity | `NOESIS_EIA_API_KEY` |
| Ember | monthly generation and demand, yearly capacity, per declared dataset release | `NOESIS_EMBER_API_KEY` |
| Eurostat | energy balances `nrg_bal_c` through the SDMX connector, flags verbatim | none |
| Energy-Charts | `public_power` only (marked `derived_from: entsoe`); prices link-only | none |

Access decisions, licences, rate limits and revision behaviour are in the
[source audit](../roadmaps/energy-systems-source-audit.md); the bounded selections are the
`energy-systems` source pack (`config/source_packs/energy.json`). Every provider is `unverified-live` until the
dated live run (#2271).

## Journey

1. **Acquire** a bounded selection: `acquire_energy_source(namespace, provider, selection, observation,
   budget_id, reuse_notice)` or run the `energy-systems` source pack. Each step is receipted; a failed step
   changes no stored value and the provider reads as stale in `energy_bundle_status`.
2. **Match identities**: `propose_energy_identity_matches` proposes candidates (countries through the declared
   code table and geospatial place resolution, zones and balancing areas as places, plants and units as
   identifier-based canonical entities). A different principal reviews each with
   `review_energy_identity_match` (`accept`, `reject`, `revert`); ambiguous and unmatched codes are listed.
3. **Ask**: `energy_generation_mix(namespace, subject, start, end, as_of_ms)` and `energy_observations` return
   per-source series for the subject and every code accepted as the same place, with related areas (for example a
   bidding zone whose members include the country) listed separately. `as_of_ms` selects the vintage published at
   or before that time. `energy_revision_history` lists every vintage of a series or one figure with release dates
   and cited changes. `energy_capacity_as_of(namespace, subject, date)` answers capacity by zone, plant or unit,
   with `unknown` and `not_in_effect` rather than guesses.
4. **Prices in market storage**: `publish_energy_prices_to_market` writes a price vintage as market bars on a
   listing the market owner registered; each bar cites the vintage. Negative prices cannot be represented in
   `noesis-market-bar-v1` and are listed as refused (they stay in the energy vintage).
5. **Link by citation**: `link_energy_records(target='climate-environment' | 'market')` links records that share
   an ENTSO-E document or are cited by market bars; `link_energy_facility` links plant series to facility records
   only through an accepted identity match. Nothing is linked by name similarity, co-location or correlation.
6. **Monitor**: `create_energy_monitor` subscribes to a subject; `run_energy_monitor` evaluates at committed
   watermarks and reports `new_release`, `revision` (old and new values, both cited), `capacity_change` and
   `threshold_crossed` for user-configured thresholds only.

## Offline evidence

`tests/unit/domains/test_energy_acceptance.py` runs the whole journey from authored fixtures with sockets blocked;
`tests/unit/energy/` covers each delivery issue. Fixtures are regenerated with
`python -m tests.unit.energy.fixture_builder`; their values are invented and are not live evidence.
