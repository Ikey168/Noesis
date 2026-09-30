# Clinical Evidence: health-system capacity

The optional `health-capacity` feature of the Clinical Evidence bundle (#2215)
takes a country or region to its published health-system capacity indicators -
hospital beds, health workforce and expenditure by financing scheme - per
source, with definitions, definition breaks, comparability notes and release
vintages, as of a chosen date, beside the surveillance series the pack already
holds for the same place. (The issue names the feature `health_capacity`;
composition feature ids are kebab-case, so the manifest declares
`health-capacity`.)

**Boundary.** Indicators are shown as each source published them, side by side.
The feature produces no health-system performance ranking or quality score, no
harmonised, adjusted or re-estimated value and no combined metric; places are
never ordered by an indicator. A definition change is a marked break; a missing
value is unknown.

## Sources

| Source | How | Revisions |
| --- | --- | --- |
| WHO Global Health Observatory | `health-capacity` connector, format `who-gho-odata`: the existing GHO OData path (`/Indicator`, `/{IndicatorCode}`), keyed by indicator code, place, year and publish state | values change in place; a changed value on re-acquisition is a new vintage (release clock `Last-Modified` or the latest value date, labelled) |
| OECD Health Statistics | `health-capacity` connector, format `oecd-sdmx-csv`: the existing SDMX connector (`SDMXConnector('OECD')`, SDMX-CSV), keyed by dataflow, version, dimension key and period; `OBS_STATUS`, source and country notes kept verbatim | the dataflow version is part of the declared document and the native revision: a new version is a new vintage |
| Eurostat health care resources and expenditure | `health-capacity` connector, format `eurostat-sdmx-csv`: the existing Eurostat path (`hlth_rs_*`, `hlth_sha11_*`) and its `OBS_FLAG` mapping | `LAST UPDATE` is the release clock; every update is a new vintage |

The `health-capacity` connector is the surveillance adapter restricted to
capacity documents (every document declares its `capacity_domain`). Access,
terms, attribution, rate limits, indicator codes, places and years are recorded
in the [source audit](../roadmaps/clinical-health-capacity-source-audit.md) and
returned by `clinical_provider_contracts` and `health_capacity_readiness`. No
source is live until the dated live run (#2481).

## Records

Capacity series live in the surveillance series and vintage storage
(`noesis-surveillance-record-v1`, `src/kb/surveillance.py`) under the condition
scheme `health-capacity` with the domain as condition code; there is no second
series or vintage store. `src/kb/health_capacity.py` composes the
`noesis-health-capacity-record-v1` views: `capacity-indicator` (source code,
unit, place, definition as published and its history), `indicator-definition`
and `definition-revision` (valid-from, the declaring release and its retrieval
time) and `capacity-observation` (place, reference period, verbatim flags,
vintage and as-of time).

Alignment records (`src/kb/health_capacity_comparability.py`): place resolutions
to Geospatial places by the published code (aggregates are never countries),
indicator mappings (`equivalent`, `broader`, `narrower` with evidence) and
comparability notes citing both definitions - all reviewable, rejectable and
revertible. Links (`src/kb/health_capacity_links.py`): co-display with
surveillance series for the same resolved place, and Economics denominators only
where the publisher cites the series.

## Journey

1. Enable the feature: select `clinical-evidence` with
   `features: ["health-capacity"]` through the composition coordinator. With the
   feature off the bundle is unchanged.
2. Acquire through the source-pack runtime (`run_source_pack_execution`, pack
   `clinical-evidence`, sources `who-gho-health-capacity`,
   `oecd-health-statistics`, `eurostat-health-care-resources`).
3. `resolve_health_capacity_places` and `review_health_capacity_place`: codes to
   places; a rejected resolution is not used.
4. `propose_health_capacity_mapping` / `review_health_capacity_mapping` and
   `record_health_capacity_note` / `review_health_capacity_note`: align
   equivalent indicators and record where definitions differ.
5. `link_health_capacity_economics`: cited denominators only.
6. Ask: `health_capacity_as_of` (place id or code, as-of date, domains) returns
   every source per domain with unit, definitions, flags, citations, breaks,
   notes and vintage differences; `health_capacity_definition_history`,
   `health_capacity_comparability`, `health_capacity_beside_surveillance`,
   `health_capacity_series_links`. A place with no data is reported as having
   none on record.
7. Watch: `create_health_capacity_monitor`, `run_health_capacity_monitor`,
   `poll_health_capacity_monitor` deliver new releases, revisions (old and new
   values, both vintages cited) and definition changes once per committed
   watermark; thresholds are user-configured only.

The offline acceptance test
`tests/unit/domains/test_health_capacity_acceptance.py` runs this journey on
authored fixtures.
