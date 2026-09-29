# Climate and Environment pack scope

Status: implemented offline, 2026-09-27 (see `docs/subsystems/climate-environment.md`). Live provider coverage is not validated. It infers no attribution, projection or compliance determination.

## Delivery state (audited 2026-09-27)

- Delivered: code for E01–E14 is in `src/ingestion/environment_providers.py`, `src/kb/environment_*.py`, `src/policy_monitor/environment_evidence.py` and `tools/knowledge_engine_mcp/environment.py`, with unit tests in `tests/unit/environment/` and the offline acceptance journey in `tests/unit/domains/test_climate_environment_acceptance.py`. Issues are not closed by this change.
- Composition: composed. `packs/climate-environment/manifest.json` binds `environment.core` plus the shared `geospatial.core`, `geospatial.transit`, `market.lei`, `platform.source-runtime` and `platform.subscriptions` providers. `set_climate_environment_bundle_enabled` is a coordinator selection change once the bundle is cut over; Geospatial keeps working when it is disabled.
- Sources: the `climate-environment` source pack (`packs/climate-environment/source_packs/climate-environment.json`) and the `geospatial-berlin` 1.2.0 upgrade adding Umweltatlas WFS layers.
- Live: the dated run (`docs/development/environment-evidence/live-check-2026-09-27.json`) reached no provider (`ConnectError` under the build environment's egress policy; OpenAQ and ENTSO-E also lacked credentials). `LIVE_VERIFICATION` is `blocked` per provider; Copernicus/CAMS is `not implemented`. E14's first criterion therefore stays open until a run from an unrestricted network.

Tracking: [#1849](https://github.com/Ikey168/Noesis/issues/1849).

## Outcome

Given a place, sector or facility, assemble source-cited environmental evidence:
air-quality and climate observation series, electricity generation, load and
outage records, industrial emissions and permits, and emissions-trading
records, projected onto geospatial places with as-of provenance. Observations,
model outputs and forecasts stay distinguished.

## Implementation issues

- [x] [#1892](https://github.com/Ikey168/Noesis/issues/1892) — E01 Audit environmental source contracts and select bounded provider coverage.
- [x] [#1893](https://github.com/Ikey168/Noesis/issues/1893) — E02 Define observation-series, facility, grid-event and indicator-vintage records.
- [x] [#1894](https://github.com/Ikey168/Noesis/issues/1894) — E03 Acquire OpenAQ and Umweltbundesamt air-quality measurements with station geometry (offline; live blocked).
- [x] [#1895](https://github.com/Ikey168/Noesis/issues/1895) — E04 Acquire ENTSO-E and SMARD generation, load and unavailability records (offline; live blocked).
- [x] [#1896](https://github.com/Ikey168/Noesis/issues/1896) — E05 Acquire EEA Industrial Emissions Portal facilities and EU ETS installation records (offline; live blocked).
- [x] [#1897](https://github.com/Ikey168/Noesis/issues/1897) — E06 Acquire Berlin Umweltatlas layers through the existing WFS source path (offline; live blocked).
- [x] [#1898](https://github.com/Ikey168/Noesis/issues/1898) — E07 Acquire Open-Meteo and DWD climate series tagged as observation, reanalysis or forecast (offline; live blocked; CAMS not implemented).
- [x] [#1899](https://github.com/Ikey168/Noesis/issues/1899) — E08 Project stations and facilities onto places and answer spatial as-of queries.
- [x] [#1900](https://github.com/Ikey168/Noesis/issues/1900) — E09 Record indicator vintages and compare revisions of environmental series.
- [x] [#1901](https://github.com/Ikey168/Noesis/issues/1901) — E10 Link facilities to corporate identity and to policy-monitor obligations.
- [x] [#1902](https://github.com/Ikey168/Noesis/issues/1902) — E11 Monitor threshold exceedances, grid unavailability and new permits.
- [x] [#1903](https://github.com/Ikey168/Noesis/issues/1903) — E12 Compose the Climate and Environment bundle over geospatial, market.lei and platform providers.
- [x] [#1904](https://github.com/Ikey168/Noesis/issues/1904) — E13 Add offline place-to-environmental-dossier acceptance coverage.
- [ ] [#1905](https://github.com/Ikey168/Noesis/issues/1905) — E14 Validate live environmental coverage and publish a cited place-dossier demo. Live check script, dated blocked report and the offline demo are delivered; live validation is blocked by the build environment's network policy.

Each issue contains acceptance criteria and prerequisite references.

## Sources

- [OpenAQ](https://docs.openaq.org/): selected locations and sensors (API key).
- [Umweltbundesamt air data](https://www.umweltbundesamt.de/daten/luft/luftdaten/doc): selected stations and components.
- [ENTSO-E Transparency Platform](https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token): generation, load and unavailability for one bidding zone (security token).
- [SMARD](https://www.smard.de/home/downloadcenter/download-marktdaten): selected chart-data files.
- [EEA Industrial Emissions Portal](https://industry.eea.europa.eu/): facilities, activities, permits and releases.
- [EU ETS Union Registry](https://climate.ec.europa.eu/eu-action/eu-emissions-trading-system-eu-ets/union-registry_en): verified emissions and allocations.
- [Berlin Umweltatlas](https://fbinter.stadt-berlin.de/fb/index.jsp): WFS layers through the geospatial-berlin source pack.
- [Open-Meteo](https://open-meteo.com/en/docs): ERA5 reanalysis (model) and ICON-D2 forecasts.
- [DWD open data](https://opendata.dwd.de/): station observations.
- Copernicus/CAMS: access decision recorded; not implemented until credentials and terms are verified.

These links establish source candidates and documented access paths, not
verified APIs or complete live coverage.

## Composition and reuse

Follows the [pack/workflow architecture](../architecture/pack-workflow-composition.md).
Reuses `src/kb/geospatial.py` (places, geometries, relations, resolver),
`src/kb/geospatial_features.py` (feature revisions, snapshots, `within`),
`src/ingestion/wfs_api.py` (Umweltatlas via source-pack upgrade), the
source-pack runtime and upgrade owners, DurableHTTP and the document store, the
pint normalisation in `src/integrations/units.py`, the economic
provider-vintage clock/basis pattern, `src/kb/entities.py` and `src/kb/lei.py`
for identity candidates, the policy monitor's assertion store and
`SubscriptionStore`. No scheduler, permission ledger, project store, spatial
store or database is added.

## Acceptance

- A reproducible journey takes a place to a cited environmental dossier:
  observation series with stations and units, grid events, facility emissions
  and permits, ETS records and corporate-link state, each with source, kind and
  as-of time visible — delivered offline (`test_climate_environment_acceptance.py`,
  `docs/examples/environment-place-dossier-demo.md`).
- Offline and bounded live evidence are reported separately (tests/demo vs
  `docs/development/environment-evidence/`); live evidence currently records
  only blocked attempts.

Attribution claims, climate projections, compliance determinations, outage
inference from news, scraping where access or terms forbid it, and
pack-asserted legal thresholds are outside this scope.
