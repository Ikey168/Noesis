# Economic provider vintages and connector readiness

Noesis uses the existing `DatasetConnector`, `ObservationStore`, and economic release store for official macro series. The connectors preserve provider-specific clocks and dimensions in `SeriesRecord.metadata`; `register_series()` projects provider vintage, public-release basis, and local acquisition time into the shared economic tables. It does not create another vintage store.

Use `harvest_with_report(query)` when readiness matters. It returns the records plus a status (`available`, `partial`, `empty`, `unavailable`, or `blocked`) and sanitized stage/code diagnostics. Diagnostics omit exception messages and request URLs so credentials cannot leak into logs or review output. The older `harvest()` remains a resilient iterator for existing callers.

## FRED and ALFRED

FRED requires `FRED_API_KEY`. A normal query retrieves the current FRED real-time view. A historical ALFRED view can be requested by an exact date:

```python
from src.ingestion.connectors.dataset.fred import FredConnector

connector = FredConnector()
records = list(
    connector.harvest(
        {
            "series": "UNRATE",
            "geography": "US",
            "vintage_date": "2020-03-01",
            "observation_start": "2019-01-01",
            "observation_end": "2020-12-31",
        }
    )
)
```

The adapter sends `realtime_start` and `realtime_end` as the same date and records that date as the provider vintage. It separately records local `acquired_at_ms`, provider `last_updated`, per-observation real-time periods, frequency, units, seasonal adjustment, and a release ID when the provider returns one or the caller supplies one. FRED's series and observation endpoints do not supply the exact official release timestamp, so `provider_release_at_ms` remains null with a reason. The shared economic model labels the release-cutoff clock as a provider-vintage fallback unless an operator supplies a validated release timestamp.

`FredConnector.series_releases(series_id)` resolves release IDs for a series; pass a verified ID to `release_dates(release_id, ...)` for source-published dates at day precision. These are not time-of-day release timestamps, and FRED notes that published release dates do not necessarily equal when data becomes available on FRED or ALFRED. Future dates may also be absent unless requested with `include_release_dates_with_no_data=True`. [FRED series-release endpoint](https://fred.stlouisfed.org/docs/api/fred/series_release.html) · [FRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) · [FRED release dates](https://fred.stlouisfed.org/docs/api/fred/release_dates.html)

## Eurostat JSON-stat

`EurostatConnector` records the requested filters, selected dimension codes and labels, update timestamp, acquisition time, units, frequency, geography, and seasonal adjustment when a recognized dimension is present. If a multi-category dimension was not explicitly selected, the existing single-series projection deterministically selects the first category and sets `dimension_coverage` to `partial`; use explicit filter parameters before treating that projection as the requested series. Selected dimension codes contribute to series identity, avoiding collisions between slices.

Eurostat's dataset `updated` clock is retained as a dataset revision timestamp. It is not treated as an official release timestamp. Multi-dimensional or unretrieved slices remain coverage gaps.

## World Bank

`WorldBankConnector` follows all result pages within configurable page and observation budgets. If the provider advertises more pages than the budget or returns a malformed later page, it rejects the series instead of publishing a truncated history. The record preserves source `lastupdated`, local acquisition time, page counts/source page URLs, units and per-period observation status/decimal metadata. The response does not provide a release time or general seasonal-adjustment classification; those values remain explicitly unknown.

## ECB, Eurostat, and Bundesbank SDMX

`SDMXConnector` preserves dimension keys, common and per-observation attributes, the structure mapping when supplied, dataset action/validity metadata, SDMX header prepared/extracted clocks, and exact raw-payload hash. The retained provider vintage is the response acquisition time because the generic connector does not request a historical SDMX vintage. Prepared/extracted clocks are not relabeled as release time. Seasonal adjustment is normalized only when a recognized dimension or common attribute identifies it; otherwise it stays unknown.

## Snapshot interpretation

Snapshots expose `release_at_basis`, `retrieved_at_basis`, `vintage_basis`, and `release_time_status` for each captured series. `connector_acquisition` means the local retrieval clock was retained. `provider_vintage_fallback` means the provider's exact publication time was unavailable and the stored vintage clock was used for release-cutoff selection; snapshot limitations repeat this qualification. These labels make fixture and source gaps visible, but do not replace a live sample, source calendar, or analyst review.

## Coverage gaps and stale series

`harvest_with_report` adds a `coverage`-stage `stale_series` diagnostic when a
series' latest non-null observation lags its acquisition by more than
`DatasetConnector.STALE_AFTER_PERIODS` (daily 10, weekly 5, monthly 4,
quarterly 3, annual 2 periods). The report status then becomes `partial`, so a
discontinued or rebased dataset is not mistaken for complete coverage. ECB
`ADJUSTMENT` codes are normalized (`N` not adjusted; `S`, `Y` adjusted;
working-day-only codes stay unknown). Explicitly bounded historical snapshots
and ALFRED vintages opt out of current-feed freshness checks; their historical
cutoff is intentional and remains recorded in vintage metadata.

## Live evidence (2026-09-24)

`scripts/market_live_macro_evidence.py` records live requests in
`config/market/acceptance_packs/live-macro-sources.json` (identifiers, clocks,
counts and diagnostics only):

| Provider | Result |
| --- | --- |
| Eurostat | `namq_10_gdp` (FR, SCA) current to 2026-Q2 with vintage, acquisition, unit and adjustment preserved; `prc_hicp_manr` (DE) ends at 2025-12 and is reported `stale_series` (Eurostat moved HICP to COICOP-2018 datasets). |
| World Bank | `NY.GDP.MKTP.CD` (US) and `FP.CPI.TOTL.ZG` (DE) complete through 2025 with `lastupdated`, pagination and per-period metadata. |
| ECB SDMX | `EXR` USD/EUR daily current to the request date; `ICP` euro-area HICP ends at 2025-12 and is reported `stale_series`. Requires the optional `sdmx1` package. |
| FRED/ALFRED | `live_verified`: current `CPIAUCSL` history plus an explicit 2020-03-01 ALFRED vintage were acquired; release membership resolved to release 10 and returned 23 source dates from 2025-01-15 through 2026-12-10 at day precision. |

Connector metadata and this sample do not establish broad provider coverage,
provider revision history across every series, or exact intraday publication
times. See the [market capability audit](../roadmaps/market-analytics-capability-audit.md) and [economic release comparison guide](economic-release-comparisons.md).
