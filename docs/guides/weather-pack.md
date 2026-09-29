# Weather pack

Operational weather as the national and aviation services published it:
station location history, observation reports with their quality-control
flags, forecast issuances, CAP warnings with their updates and cancellations,
and a verification of published forecasts against the observations recorded
later. Tracking issue: #2163. Source decisions are recorded in the
[source audit](../development/weather-evidence/source-audit.md). The record
shape is the [weather record contract](../contracts/weather-records.md).

Noesis runs no forecast model, blends nothing and gives no safety, travel,
agricultural or aviation advice. A warning is quoted as its issuer published
it, with its validity.

## Composition

`packs/weather/pack.json` (v1) and `packs/weather/composition.json` compose:

| Provider | What it serves |
| --- | --- |
| `weather.observations` | the record owner (`src/kb/weather_store.py`), observations queries, station identity, cited evidence links |
| `weather.forecasts` | `forecast_as_issued`, `forecast_evolution` |
| `weather.warnings` | `warnings_in_force`, weather monitors |
| `weather.verification` | `verify_published_forecasts`, `replay_weather_verification` (optional `weather-verification` feature, default off) |

The bundle requires Climate & Environment (`environment.records`: stations stay
environment `station` records), Geospatial (places, features, containment and
proximity receipts), `platform.entity-identity`, `platform.subscriptions` and
`platform.source-acquisition`. The `weather-open-meteo` feature (default off)
gates Open-Meteo runs: the free API is non-commercial only, and in a composed
deployment the projector refuses Open-Meteo pages unless the feature is
selected.

## Acquisition

`config/source_packs/weather.json` (`weather-operational` 1.0.0) runs through
the source-pack runtime with the `weather` connector
(`src/ingestion/weather_sources.py`). DWD and Open-Meteo reuse the Climate &
Environment plans, parsers and host allow-list. Every request sends the
declared `User-Agent`. Every source is `unverified-live` until a dated run is
recorded under `docs/development/weather-evidence/` (WX14).

## Questions and tools

| Question | Tool | Semantics |
| --- | --- | --- |
| What was observed at a station or place? | `weather_observations` | current revision per report among revisions acquired by `knowledge_cutoff`; QC flag verbatim with a common state; the location vintage valid at the report's time; "no report on record" when empty |
| What was forecast for a time, as issued before a cutoff? | `forecast_as_issued` | per provider, product and model the latest issuance at or before `issued_before` that covers the valid time, with lead time |
| How did the forecast for a time evolve? | `forecast_evolution` | every issuance with an element at the valid time |
| Which warnings were in force? | `warnings_in_force` | CAP chains threaded by references in any arrival order; the latest message sent by `as_of` decides; areas by Geospatial containment |
| How did published forecasts verify? | `verify_published_forecasts` | stated pairing rule and tolerance; bias, MAE, RMSE, Brier score and user-threshold contingency metrics with definitions, sample sizes and exclusions; no ranking |
| Which station ids are the same site? | `propose_weather_station_matches`, `review_weather_station_match`, `revert_weather_station_match`, `list_weather_station_matches` | source-stated identifiers link; proximity only proposes; another principal reviews; reverts never reactivate |
| Show weather beside a Climate dossier or event | `attach_weather_evidence`, `review_weather_evidence_link` | a reviewed, revertible citation; nothing is written into the target |
| Tell me when something changes | `create_weather_monitor`, `run_weather_monitor`, `poll_weather_monitor` | knowledge subscriptions at committed watermarks; issuer text quoted, no advice |

Each tool declares every scope it always uses. The place arguments of
`weather_observations` also need `knowledge:geospatial:calculate`, which is
checked when they are given. Before any weather source has run in a namespace,
every entry point returns `not_ready`.

## Offline evidence

`tests/unit/domains/test_weather_acceptance.py` runs the whole journey with
sockets blocked, on fictional stations (`tests/unit/weather/fixture_builder.py`).
The unit tests under `tests/unit/weather/` cover each delivery issue.
