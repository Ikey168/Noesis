# Climate and Environment provider fixtures

**Authored fixtures, not live captures.** Every file here was written by hand to
mirror the documented response *shapes* that the adapters in
`src/ingestion/environment_providers.py` (and, for Umweltatlas, the existing WFS
adapter `src/ingestion/wfs_api.py`) target. Station, facility, installation,
unit and document identifiers are illustrative; every measured, reported,
modelled or forecast **value is invented**. Facility and operator names carry an
"(authored fixture)" / "Beispiel" / "Fixture" marker and say nothing about any
real installation, operator, permit, emission or compliance record.
Coordinates are real Berlin locations (Alexanderplatz, Brückenstraße,
Frankfurter Allee, Köpenicker Straße) so the spatial journey is realistic.

`tests/unit/environment/fixture_builder.py` routes each planned request to these
files and writes the pinned native-page fixtures under
`tests/fixtures/source_packs/environment-*.json` and `umweltatlas-*.json`, then
pins their hashes in `packs/climate-environment/source_packs/*.json`.
`test_fixtures_are_pinned_and_in_sync_with_the_authored_raw_files` fails when
they drift.

Live coverage is validated separately by `scripts/environment_live_check.py`,
whose output (`docs/development/environment-evidence/`) records provider,
timestamp and failures apart from this offline evidence.

| File | Provider shape | Used for |
| --- | --- | --- |
| `openaq_location_2993.json` | OpenAQ v3 `/locations/{id}` | station with EEA code in the name, sensors, licences |
| `openaq_sensor_7771_hours.json`, `openaq_sensor_7772_hours.json` | OpenAQ v3 `/sensors/{id}/hours` | hourly NO2/PM10 observations (exact decimal text) |
| `uba_stations.json`, `uba_components.json` | UBA Luftdaten v3 `stations/json`, `components/json` | stations with EEA code, component units; unselected station ignored |
| `uba_measures_282_5.json`, `uba_measures_271_5.json` | UBA `measures/json` | CET timestamps, provisional values, a published missing value |
| `uba_measures_282_5_validated.json` | same request, later validated publication | provisional → validated vintage |
| `entsoe_a75_generation.xml` | ENTSO-E GL_MarketDocument A75/A16 | generation per production type, PT15M |
| `entsoe_a65_load.xml`, `entsoe_a65_load_forecast.xml` | GL_MarketDocument A65/A16 and A65/A01 | actual load vs day-ahead forecast (issue time) |
| `entsoe_a80_planned.xml`, `entsoe_a80_unplanned.xml` | Unavailability_MarketDocument (delivered zipped) | planned (A53) and unplanned (A54) outages, reason text, capacity, curve A03 |
| `entsoe_a80_unplanned_rev2.xml` | same document, revision 2 | updated unavailability event |
| `entsoe_ack_no_data.xml` | Acknowledgement_MarketDocument, reason 999 | "no matching data" is coverage, not an outage |
| `smard_410_DE_quarterhour_1789941600000.json`, `..._revised.json` | SMARD chart_data weekly file | realised load, trailing null, revised totals → vintage |
| `smard_411_DE_quarterhour_1789941600000.json` | SMARD chart_data (filter 411) | forecast load with `meta_data.created` |
| `eea_industry_berlin_2024.json` | EEA Discodata SQL JSON (`results`) | facilities, E-PRTR activity, ETS identifier, permit link, releases with method M/C/E |
| `eu_ets_verified_2025.csv`, `eu_ets_verified_2025_corrected.csv` | Union Registry verified-emissions table (as CSV) | verified emissions, allocations, compliance code as published; corrected vintage |
| `open_meteo_archive_era5.json` | Open-Meteo Historical Weather API | ERA5 reanalysis (kind model), grid-cell coordinates |
| `open_meteo_meta_icon_d2.json`, `open_meteo_forecast_icon_d2.json` | Open-Meteo model `meta.json` + Forecast API | forecast with model run (issue) time |
| `TU_Stundenwerte_Beschreibung_Stationen.txt` | DWD CDC station description | station 00399 Berlin-Alexanderplatz |
| `produkt_tu_stunde_20250324_20260924_00399.txt` | DWD CDC hourly product (zipped by the builder) | UTC timestamps, QN quality, `-999` missing value, "recent" = provisional |
| `umweltatlas_umweltzone.geojson`, `umweltatlas_laerm_lden.geojson`, `umweltatlas_klimafunktion.geojson` | WFS 2.0.0 GeoJSON in EPSG:25833 | Umweltatlas layers (simplified boxes) via the geospatial-berlin upgrade |
