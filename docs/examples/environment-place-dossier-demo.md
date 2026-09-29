# Climate and Environment — place dossier demo (Berlin Alexanderplatz)

> **Evidence: offline authored fixtures only.** Every provider payload comes from `tests/fixtures/environment` (hand-written in the providers' documented response shapes; identifiers are illustrative and all values are invented). They are **not** live captures and say nothing about current air quality, grid state, emissions or compliance. The dated live check (`docs/development/environment-evidence/live-check-2026-09-27.json`) could not reach any provider, so no live evidence exists yet.
>
> Observations, model output (reanalysis) and forecasts are listed separately and never mixed. No attribution, climate projection or compliance determination is made; no threshold is asserted.

Regenerate with `python scripts/environment_demo.py --output docs/examples/environment-place-dossier-demo.md` (as of 2026-09-26 09:09 UTC).

Place: **Alexanderplatz** [13.4132, 52.5219] (place `place:9a29044613fa431eb5bb36b7`), district boundary **Mitte** (Geoportal Berlin ALKIS fixture), bidding zone **DE-LU** (authored coarse outline). Dossier status: `complete`; replay deterministic: `True` over 7 spatial receipts.

## Air quality and weather observations (stations within Mitte)

| Station | Provider | Indicator | Kind | Unit | Latest values (UTC) | Vintage status | Retrieved | Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DEBE068 | openaq | pm10 | observation | µg/m³ | 00:00 14.2, 01:00 12.8, 02:00 16.5, 03:00 19.0 | unknown | 2026-09-26 09:01 UTC | [link](https://explore.openaq.org/locations/2993) |
| DEBE068 | openaq | no2 | observation | µg/m³ | 00:00 31.4, 01:00 28.9, 02:00 44.7, 03:00 52.1 | unknown | 2026-09-26 09:01 UTC | [link](https://explore.openaq.org/locations/2993) |
| ↳ linked, not merged: shared EEA station code (1.3 m) | | | | | | | | |
| Berlin-Alexanderplatz | dwd | TT_TU | observation | °C | 00:00 12.6, 01:00 12.1, 02:00 11.8, 03:00 n/a | provisional | 2026-09-26 09:09 UTC | [link](https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/recent/stundenwerte_TU_00399_akt.zip) |
| Berlin-Alexanderplatz | dwd | RF_TU | observation | % | 00:00 81.0, 01:00 83.0, 02:00 84.0, 03:00 85.0 | provisional | 2026-09-26 09:09 UTC | [link](https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/recent/stundenwerte_TU_00399_akt.zip) |
| Berlin Mitte | uba | NO2 | observation | µg/m³ | 00:00 30, 01:00 29, 02:00 45, 03:00 53 | provisional | 2026-09-26 09:02 UTC | [link](https://www.umweltbundesamt.de/en/data/air/air-data) |
| ↳ linked, not merged: shared EEA station code (1.3 m) | | | | | | | | |

## Model output (reanalysis) for the place's grid cell

| Series | Model | Grid | Kind | Latest values | Source |
| --- | --- | --- | --- | --- | --- |
| precipitation (mm) | ERA5 | 25000 m, 6359 m away | model | 00:00 0.0, 01:00 0.0, 02:00 0.2, 03:00 0.1 | [link](https://open-meteo.com/en/docs) |
| temperature_2m (°C) | ERA5 | 25000 m, 6359 m away | model | 00:00 12.1, 01:00 11.6, 02:00 11.2, 03:00 10.9 | [link](https://open-meteo.com/en/docs) |

## Forecasts

| Series | Model | Issue time | Kind | Values | Source |
| --- | --- | --- | --- | --- | --- |
| temperature_2m (°C) | DWD ICON-D2 | 2026-09-26T03:00:00Z | forecast | 09:00 12.8, 10:00 12.3, 11:00 11.9, 12:00 11.5 | [link](https://open-meteo.com/en/docs) |
| precipitation (mm) | DWD ICON-D2 | 2026-09-26T03:00:00Z | forecast | 09:00 0.0, 10:00 0.1, 11:00 0.3, 12:00 0.0 | [link](https://open-meteo.com/en/docs) |

## Grid records for bidding zone DE-LU

| Record | Provider | Type | Kind | Detail | As of | Source |
| --- | --- | --- | --- | --- | --- | --- |
| Day-ahead total load forecast (10Y1001A1001A82H) | entsoe | load | forecast | PT15M, MW: 44100, 43800, 43450 … | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/) |
| Actual total load (10Y1001A1001A82H) | entsoe | load | observation | PT15M, MW: 44493, 43950, 43504 … | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/) |
| Actual generation – Fossil Brown coal/Lignite (10Y1001A1001A82H) | entsoe | generation | observation | PT15M, MW: 8412, 8398, 8377 … | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/) |
| Actual generation – Wind Onshore (10Y1001A1001A82H) | entsoe | generation | observation | PT15M, MW: 14250, 14102, 13987 … | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/) |
| Unavailability of Fixture Block A | entsoe | unavailability | observation | planned, Fixture Block A 500 MW nominal, 2026-09-23T22:00:00Z → 2026-09-27T22:00:00Z; reason: Planned maintenance (revision of boiler) as published by the fixture producer | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/outage-domain/r2/unavailabilityOfProductionAndGenerationUnits/show) |
| Unavailability of Fixture GT 1 | entsoe | unavailability | observation | unplanned, Fixture GT 1 300 MW nominal, 2026-09-24T01:40:00Z → 2026-09-24T18:00:00Z; reason: Failure: gas turbine trip, cause under investigation (as published) | 2026-09-26 09:03 UTC | [link](https://transparency.entsoe.eu/outage-domain/r2/unavailabilityOfProductionAndGenerationUnits/show) |
| SMARD Stromverbrauch: Gesamt (Netzlast) (DE, quarterhour) | smard | load | observation | PT15M, MWh: 11123.25, 10987.5, 10876.0 … | 2026-09-26 09:04 UTC | [link](https://www.smard.de/home/downloadcenter/download-marktdaten) |
| SMARD Prognostizierter Stromverbrauch: Gesamt (DE, quarterhour) | smard | load | forecast | PT15M, MWh: 11050.0, 10950.0, 10850.0 … | 2026-09-26 09:04 UTC | [link](https://www.smard.de/home/downloadcenter/download-marktdaten) |

## Facilities near the place (5 km)

### HKW Mitte (authored fixture) — 1543 m

- Source: [eea-industry](https://industry.eea.europa.eu/); operator as published: *Beispiel Wärme Berlin GmbH* (unmatched (source string))
- Activity: Thermal power stations and other combustion installations
- Permits (as published): https://www.berlin.de/fixture/permits/hkw-mitte.pdf
- Release NOX to AIR: 187500.5 kg (2024, method M, kind observation)
- Release CO2 to AIR: 612000000 kg (2024, method C, kind observation)
- EU ETS HKW Mitte (authored fixture): allocated_allowances 41250 allowances (2025); compliance code as published: A
- EU ETS HKW Mitte (authored fixture): verified_emissions 598412 tCO2e (2025); compliance code as published: A
- reported data as published; no compliance determination is made

## Umweltatlas layers containing the place

| Layer | Feature | Properties (as published) |
| --- | --- | --- |
| ua_umweltzone:umweltzone | Umweltzone Berlin (authored fixture outline) | {'bezeichnung': 'Umweltzone Berlin (authored fixture outline)', 'gueltig_ab': '2010-01-01', 'stufe': 'Stufe 3 (grüne Plakette)'} |
| ua_stratlaerm_2022:laerm_lden | > 65 - 70 | {'einheit': 'dB(A)', 'grundlage': 'Strategische Lärmkarte 2022, Berechnung (kein Messwert)', 'lden_klasse': '> 65 - 70'} |
| ua_klimaanalyse_2022:klimafunktion | Überwärmungsbereich, hohe Belastung | {'grundlage': 'Klimaanalysekarte 2022, Modellrechnung (FITNAH 3D)', 'klimafunktion': 'Überwärmungsbereich, hohe Belastung'} |

## Coverage gaps and not implemented

- none in this offline dossier
- copernicus-cams: not implemented: ADS account, key and licence acceptance unverified

## Provider state (offline vs live)

| Provider | This dossier's evidence | Live verification |
| --- | --- | --- |
| dwd | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| eea-industry | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| entsoe | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| open-meteo-archive | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| open-meteo-forecast | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| openaq | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| smard | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
| uba | injected fixture (injected) | blocked (2026-09-27, ConnectError) |
