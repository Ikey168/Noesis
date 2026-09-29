# Weather: operational source-contract audit and bounded coverage (WX01, #2164)

Tracking issue: #2163. This audit records, for each candidate operational
weather source, the access route, authentication, terms and attribution text,
rate limits and the User-Agent requirement, stable identifiers, issuance
cadence, how corrections appear, and the access decision the Weather pack
implements (`implement`, `link-only` or `not implemented`, with the reason).

**Verification status.** No live request was made while writing this audit.
The build environment has no network access to DWD Open Data, the NWS API,
aviationweather.gov or Open-Meteo. Every claim marked **(verify)** comes from
the providers' public documentation as known at the time of writing. Each one
must be checked against the live terms and services before live acquisition is
accepted (WX14, #2177). The adapters in `src/ingestion/weather_sources.py`
parse the documented formats and fail closed (`schema_drift`) on any other
shape. The offline fixtures under `tests/fixtures/weather/` are authored. They
name fictional stations with realistic identifier shapes (a five-digit DWD
station id, a WMO block number, a four-letter ICAO code, an NWS office grid)
and are never captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in `src/ingestion/weather_sources.py`
restate these decisions in code. Every implemented source is `unverified-live`
until a dated live run is recorded under this directory.

## Summary

| Source | Endpoint | Decision |
| --- | --- | --- |
| DWD CDC 10-minute station observations (`observations_germany/climate/10_minutes/air_temperature`, `recent` and `historical`) with `QN` | `https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/` | `implement` (unverified-live) |
| DWD CDC hourly station observations (`observations_germany/climate/hourly/air_temperature`) with `QN_9` | `https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/` | `implement` (unverified-live), through the Climate & Environment `dwd` plan and parser |
| DWD CDC station descriptions and station geography history (`*_Beschreibung_Stationen.txt`, `Metadaten_Geographie_<id>.txt` inside the product ZIP) | same hosts | `implement` (unverified-live) |
| DWD MOSMIX_L single-station KMZ (`weather/local_forecasts/mos/MOSMIX_L/single_stations/<id>/kml/`) | `https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/` | `implement` (unverified-live) |
| DWD MOSMIX_S | `https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_S/` | `not implemented`: MOSMIX_S is published only as one all-station file (tens of MB per hourly run **(verify)**), which exceeds the per-source byte budget. No bounded per-station path exists **(verify)** |
| DWD MOSMIX station catalogue (`mosmix_stationskatalog.cfg`) | `https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg` **(verify)** | `implement` (unverified-live), for source-stated MOSMIX id ↔ ICAO cross-identifiers |
| DWD CAP warnings (`weather/alerts/cap/DISTRICT_DWD_STAT/`, latest ZIP of CAP 1.2 messages) | `https://opendata.dwd.de/weather/alerts/cap/` | `implement` (unverified-live) |
| aviationweather.gov Data API: METAR (`/api/data/metar`) | `https://aviationweather.gov/api/data/` | `implement` (unverified-live) |
| aviationweather.gov Data API: TAF (`/api/data/taf`) | same | `implement` for the issuance (raw text, issue time, validity period); decoded change groups `not implemented` (reason below) |
| aviationweather.gov Data API: station info (`/api/data/stationinfo`) | same | `implement` (unverified-live), for source-stated ICAO ↔ WMO cross-identifiers |
| NWS API `/points/{lat},{lon}` | `https://api.weather.gov/` | `implement` (unverified-live): the point → office grid mapping is recorded |
| NWS API gridpoint hourly forecast (`/gridpoints/{wfo}/{x},{y}/forecast/hourly`) | same | `implement` (unverified-live) |
| NWS API alerts (`/alerts?zone=…`) | same | `implement` (unverified-live) |
| NWS API station observations (`/stations/{id}/observations`) | same | `link-only`: the same reports are acquired as METAR from aviationweather.gov. Two copies of one report would need their own reconciliation, so v1 keeps one path |
| NWS raw gridpoint data (`/gridpoints/{wfo}/{x},{y}`) | same | `not implemented`: the hourly forecast covers the bounded parameters. The raw layers use interval-valued ISO 8601 durations that v1 does not need |
| Open-Meteo Forecast API with a pinned model (`/v1/forecast?models=icon_d2`) and the model's `meta.json` | `https://api.open-meteo.com/` | `implement`, gated behind the default-off `weather-open-meteo` feature (terms decision below) |
| Personal weather stations and crowdsourced networks | n/a | `not implemented` (tracker exclusion) |

## Boundary against the Climate & Environment pack

The Climate & Environment pack (`src/ingestion/environment_providers.py`,
`packs/climate-environment/`) already acquires:

* **DWD CDC hourly `air_temperature`** (`TT_TU`, `RF_TU`) per station and
  period (`recent` or `historical`). These are stored as
  `observation_series` records with vintages in `src/kb/environment_store.py`,
  and the station description is stored as an environment `station` record.
* **Open-Meteo Historical Weather API** (ERA5 and ERA5-Land reanalysis, kind
  `model`) and the **Open-Meteo Forecast API** for `icon_d2` as one
  `observation_series` per run (kind `forecast`, issue time from `meta.json`).

The Weather pack adds only operational products and never re-creates those
records:

* **Stations stay owned by the environment `station` record type.** A DWD,
  MOSMIX or METAR station that the environment owner does not yet know is
  registered **through** `EnvironmentStore.apply` with provider `dwd`,
  `dwd-mosmix` or `aviationweather`. A station the environment owner already
  holds is only referenced. Its identifiers are never rewritten.
* **Location vintages** (the published geography history, with dates) are
  Weather records. The environment station keeps its current point.
* **Observation reports** (one per station, report type and observation time,
  with every parameter's QC flag verbatim) are Weather records. Where the same
  DWD parameter exists as a Climate `observation_series` (hourly `TT_TU`,
  `RF_TU`), the report cites that series by record id (WX10). The series is
  never copied.
* **Forecast issuances and elements** (MOSMIX_L runs, NWS gridpoint forecasts,
  TAF, Open-Meteo runs) are Weather records. The Climate pack's own
  Open-Meteo forecast series stays owned there. The Weather pack reuses the
  `open-meteo-forecast` plan, model table and parsers, and stores the run as an
  issuance vintage.
* **Warnings, METAR reports, QC flags and verification pairs** are Weather
  records only.

Long-term climate questions (reanalysis, climate normals, emissions) stay in
Climate & Environment.

## DWD Open Data (CDC observations, station descriptions)

* **Access.** Anonymous HTTPS file server, no authentication. The existing
  Climate contract (`PROVIDER_CONTRACTS["dwd"]`) and host allow-list
  (`PROVIDER_HOSTS["dwd"] = {"opendata.dwd.de"}`) are reused. There is no second
  DWD client.
* **Terms and attribution.** DWD open data is provided under the
  GeoNutzV / CC BY 4.0 **(verify)**. Attribution text recorded on every
  value: `Quelle: Deutscher Wetterdienst` (source: Deutscher Wetterdienst)
  **(verify)**.
* **Rate limits.** None documented **(verify)**. One file per station,
  parameter and period. The byte budget is enforced per source.
* **Identifiers.** A five-digit DWD station id (`Stations_id`, zero-padded,
  e.g. `00433`). WMO block numbers are not in the CDC files. MOSMIX uses
  WMO-like ids (`10384`) or alphanumeric ids, and its catalogue states ICAO
  codes **(verify)**.
* **Files.** 10-minute products `10minutenwerte_TU_<id>_akt.zip` (`recent`) and
  `10minutenwerte_TU_<id>_<from>_<to>_hist.zip` (`historical`) **(verify)**,
  containing `produkt_zehn_min_tu_<from>_<to>_<id>.txt` with columns
  `STATIONS_ID;MESS_DATUM;QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor` **(verify)**.
  `MESS_DATUM` is `YYYYMMDDHHMM` in UTC for data after 2000 **(verify)**.
  Hourly products use `QN_9`, `TT_TU` and `RF_TU` with `MESS_DATUM` as
  `YYYYMMDDHH` (UTC), as the Climate parser already documents. Missing values
  are `-999`. They are stored as **absent**, never as a number.
* **Station geography history.** Each product ZIP carries
  `Metadaten_Geographie_<id>.txt` with rows `Stations_id;Stationshoehe;
  Geogr.Breite;Geogr.Laenge;von_datum;bis_datum;Stationsname` **(verify)**.
  Each row is one `station_location_vintage`, with `von_datum`/`bis_datum` as
  published (an empty `bis_datum` is an open vintage, not a default date).
  A relocation adds a row. Observations are never re-located: each report
  resolves its location from the vintage valid at its own observation time.
* **Quality levels (`QN`, `QN_9`), kept verbatim.** DWD documents these levels
  **(verify)**: `1` only formal control; `2` controlled with individually
  defined criteria; `3` automatic control and correction (ROUTINE); `5`
  historic, subjective procedures; `7` second control done, before correction;
  `8` quality control outside ROUTINE; `9` not all parameters corrected; `10`
  quality control finished, all corrections finished. The common vocabulary
  (`src/kb/weather_normalise.py`) maps `10` → `passed`, `1` → `provisional`,
  and `2, 3, 5, 7, 8, 9` → `checked_partial`. Any other level maps to
  `unknown`. The native level is always retained.
* **Recent vs. historical, and corrections.** `recent` files are "not yet
  completely quality controlled", and `historical` files are quality
  controlled. A historical release that revises a value is **appended** as a
  revision of the same observation report. Precedence is `historical` over
  `recent`, and within a tier the later acquisition wins. So a late `recent`
  file acquired after the historical release never becomes current and never
  raises a correction event.
* **Cadence.** Recent files are updated daily **(verify)**.
* **Bounded v1 coverage.** Stations `00433` (Berlin-Tempelhof) and `03987`
  (Potsdam) **(verify)**. Parameter group `air_temperature`: 10-minute
  `TT_10` and `RF_10`, hourly `TT_TU` and `RF_TU`. `recent` files, plus a
  historical file only when its name is pinned in the selection.

## DWD MOSMIX

* **Access.** Anonymous HTTPS. MOSMIX_L single-station files
  `MOSMIX_L/single_stations/<id>/kml/MOSMIX_L_LATEST_<id>.kmz` **(verify)**,
  each a ZIP holding one KML document.
* **Terms and attribution.** As for DWD Open Data: CC BY 4.0 / GeoNutzV,
  attribution `Quelle: Deutscher Wetterdienst` **(verify)**.
* **Format.** The KML has `dwd:ProductDefinition` with `dwd:IssueTime`
  (UTC), `dwd:ProductID`, `dwd:GeneratingProcess` and
  `dwd:ForecastTimeSteps/dwd:TimeStep`, plus one `kml:Placemark` per station:
  `kml:name` (station id), `kml:description` (name),
  `kml:ExtendedData/dwd:Forecast[@dwd:elementName]/dwd:value` (whitespace-separated
  values aligned with the time steps, `-` for missing) and `kml:Point/kml:coordinates`
  (`lon,lat,height`) **(verify)**.
* **Issuance semantics.** Each run is one `forecast_issuance` keyed by
  provider, product, station and `IssueTime`. It is never overwritten by the
  next run. Re-acquiring the same run adds nothing. MOSMIX_L runs are issued
  four times a day (00, 06, 12, 18 UTC) **(verify)**.
* **Units (verify).** `TTT` and `Td` in K, `FF` and `FX1` in m/s, `DD` in
  degrees, `PPPP` in Pa, `RR1c` in kg/m² (= mm, one hour), `N` in %, `R101`
  in % (probability of more than 0.1 mm in one hour). Probabilities are
  published forecasts and are verified as published (Brier score). No
  threshold of our own is invented.
* **Bounded v1 coverage.** MOSMIX ids `10384` and `10379` **(verify)**.
  Elements `TTT`, `Td`, `FF`, `DD`, `PPPP`, `RR1c`, `N` and `R101`. The
  latest run per fetch, so each acquisition captures one issuance vintage.
* **MOSMIX_S:** `not implemented` (see summary).

## DWD CAP warnings

* **Access.** Anonymous HTTPS. Latest-state ZIP
  `weather/alerts/cap/DISTRICT_DWD_STAT/Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMDWD_DISTRICT_DE.zip`
  **(verify)**, holding one CAP 1.2 XML document per message.
* **Terms and attribution.** DWD open data terms. Warnings are quoted with
  their issuer `Deutscher Wetterdienst` and their validity **(verify)**.
* **Identifiers.** A CAP `identifier` (e.g. `2.49.0.0.276.0.DWD.PVW.<ms>.<uuid>`
  **(verify)**) under `sender` (`opendata@dwd.de` **(verify)**). Areas carry
  `geocode` `WARNCELLID` values (nine digits) and `polygon` rings.
* **Corrections and updates.** `msgType` is `Alert`, `Update` or `Cancel`.
  An `Update` or `Cancel` lists the messages it replaces in `references` as
  `sender,identifier,sent` triples, separated by whitespace. Chains are
  computed at read time from the stored references, so arrival order does not
  matter.
* **Area projection.** Warning areas project to Geospatial features by
  **warncell id** (`native_id = warncell:<id>`), never by area name. The
  polygon comes from the message. A changed polygon for the same warncell is a
  new feature revision.
* **Bounded v1 coverage.** Warncells `111000000` (Berlin) and `112054000`
  (Potsdam) **(verify)**. Messages for other warncells in the ZIP are counted
  as out of scope and not stored.
* **No advice.** `instruction` and `description` are kept verbatim as the
  issuer's text. Noesis adds none of its own.

## aviationweather.gov Data API (METAR, TAF, station info)

* **Access.** `https://aviationweather.gov/api/data/metar?ids=…&format=json&hours=…`,
  `/taf?ids=…&format=json` and `/stationinfo?ids=…&format=json` **(verify)**.
  No authentication.
* **User-Agent and rate limits.** The Data API asks clients for a
  descriptive `User-Agent` and limits request rates. A limit of 100 requests
  per minute is documented **(verify)**. The adapter sends
  `User-Agent: noesis-weather-pack/1.0 (+operator contact)` **(verify the
  expected form)**, and each request leaves a receipt with its status and hash.
* **Terms and attribution.** U.S. Government work, public domain. Attribution
  `NOAA / National Weather Service, Aviation Weather Center` is recorded
  **(verify)**.
* **Identifiers.** ICAO (`icaoId`), plus `wmoId`, `iataId` and `faaId` in
  station info **(verify)**.
* **METAR fields (verify).** `icaoId`, `receiptTime`, `obsTime` (epoch
  seconds), `reportTime`, `temp` and `dewp` (°C), `wdir` (degrees or `VRB`),
  `wspd` and `wgst` (kt), `visib` (statute miles, may be `10+`), `altim`
  (hPa), `metarType` (`METAR` or `SPECI`), `rawOb`, `lat`, `lon`, `elev`,
  `name`. The raw text is kept **verbatim**. Decoded fields are stored as
  published, without re-decoding.
* **Corrections.** A corrected report carries `COR` in its raw text **(verify)**.
  It is appended as a revision of the same report (station, report type and
  observation time), marked `correction: COR`. The `receiptTime` orders
  revisions. A late-arriving copy of the original never displaces the
  correction.
* **QC.** The API's `qcField` is kept verbatim. Its bit semantics are not
  confirmed **(verify)**, so it maps to `unknown`. The only exception is the
  `$` maintenance indicator in the raw text, which maps to `suspect`.
* **TAF.** `implement` for the issuance only: `issueTime`, `validTimeFrom`,
  `validTimeTo` and `rawTAF` verbatim, as a `forecast_issuance` with no
  elements. The decoded change groups (`BECMG`, `TEMPO`, `PROB30/40`) are
  `not implemented`. They are conditional period forecasts that a
  point-valued `forecast_element` cannot represent faithfully, so TAFs take no
  part in verification.
* **Bounded v1 coverage.** `EDDB` and `KBOS` **(verify)**. METAR over the
  last 3 hours per request. The TAF currently published.

## NWS API (`api.weather.gov`)

* **Access.** Anonymous HTTPS, GeoJSON (`application/geo+json`).
* **User-Agent.** Required: requests without an identifying `User-Agent` are
  rejected **(verify)**. The adapter always sends the declared one. Rate limits
  are not published as a number. Clients are asked to be modest and to honour
  HTTP 429 with `Retry-After` **(verify)**. The runtime's `rate_limited`
  failure carries `Retry-After`.
* **Terms and attribution.** U.S. Government work, public domain.
  Attribution `NOAA / National Weather Service` **(verify)**.
* **`/points/{lat},{lon}`** returns `gridId`, `gridX`, `gridY`,
  `forecastHourly` and `observationStations` **(verify)**. The mapping from
  the point to the grid is recorded with the request receipt.
* **Gridpoint hourly forecast.** `properties.updateTime` is the issuance time
  (the forecaster's last update), and `generatedAt` (when the API built the
  response) is recorded separately **(verify)**. `periods[]` carries
  `startTime`, `endTime`, `temperature` with `temperatureUnit` (`F`),
  `windSpeed` as text (`"10 mph"`), `windDirection` and
  `probabilityOfPrecipitation{unitCode,value}` **(verify)**. One issuance per
  distinct `updateTime`. The grid cell's polygon is the forecast location. The
  station declared for the grid point in the source selection is recorded, not
  inferred.
* **Alerts.** `/alerts?zone=<UGC>` returns CAP-derived GeoJSON features with
  `id`, `sender`, `sent`, `effective`, `onset`, `expires`, `ends`,
  `messageType`, `severity`, `urgency`, `certainty`, `event`, `headline`,
  `description`, `instruction`, `geocode{SAME,UGC}`, `affectedZones` and
  `references[{identifier,sender,sent}]` **(verify)**. Update and cancel
  chains are threaded exactly as for DWD CAP. Areas project by UGC zone code
  (`native_id = ugc:<code>`) when the feature carries a polygon.
* **Bounded v1 coverage.** One point, Boston (`42.3601,-71.0589`, grid `BOX`,
  declared station `KBOS`) **(verify)**. Alerts for zone `MAZ015` **(verify)**.

## Open-Meteo (terms decision)

* **Access.** `https://api.open-meteo.com/v1/forecast` with `models=icon_d2`
  and the model's `https://api.open-meteo.com/data/dwd_icon_d2/static/meta.json`
  (`last_run_initialisation_time`), exactly as the existing
  `open-meteo-forecast` contract in `src/ingestion/environment_providers.py`.
  The Weather adapter calls that module's `plan` and its parsers.
* **Terms.** The free API is for **non-commercial use** with a fair-use limit
  of 10,000 calls per day. Commercial use requires a subscription (API key and
  a different host). The data is licensed **CC BY 4.0** and needs attribution
  to Open-Meteo and the underlying model's originator **(verify)**.
* **Decision: `implement`, gated.** The adapter sits behind the optional
  `weather-open-meteo` composition feature, which is **off by default**. The
  source-pack licence carries `use: non-commercial`, and an operator must
  accept it through the existing source-pack licence acceptance before any
  run. This is the same basis as the Climate contract (`authentication: none
  (non-commercial use)`). A commercial deployment needs its own terms and host,
  and v1 does not provide that. Every value records `Open-Meteo (CC BY 4.0)`
  and the model originator (`Deutscher Wetterdienst (ICON-D2)`).
* **Semantics.** Grid-point values are **model grid values** (location kind
  `grid-cell`), never station observations. The issuance time is the model's
  `meta.json` run time. A fetch that returns the same run is deduplicated.
* **Bounded v1 coverage.** One grid point near Berlin-Tempelhof
  (`52.4675,13.4021`) with declared station `dwd:00433` **(verify)**.
  Variables `temperature_2m` and `relative_humidity_2m`, one forecast day.

## Timestamps

| Record | Reference time (source) | Valid time | Acquisition time |
| --- | --- | --- | --- |
| Observation report | observation time (`MESS_DATUM`, `obsTime`) | = observation time | `retrieved_at` per revision |
| Location vintage | `von_datum` (valid from), `bis_datum` (valid to) | the published interval | per revision |
| Forecast issuance | `IssueTime`, `updateTime`, `issueTime`, `meta.json` run time | element `valid_time` (lead = valid − issued) | per revision |
| Warning | CAP `sent` | `onset`/`effective` to `expires` | per revision |

The three clocks are stored separately as UTC ISO strings and epoch
milliseconds, and are compared as times. As-of queries select only revisions
acquired at or before the knowledge cutoff. Among those, the current revision
is chosen by the source's own time (precedence tier, then source time), with
acquisition order as the fallback.

## What remains to verify live (WX14)

The exact 10-minute file names and columns, the `Metadaten_Geographie` column
set, the MOSMIX KMZ path and element units, the MOSMIX catalogue URL and
format, the CAP ZIP path and `WARNCELLID` values for the bounded regions, the
aviationweather.gov JSON field names and `qcField` semantics, the NWS
`updateTime` semantics, `User-Agent` form and zone codes, and the Open-Meteo
terms at the time of use.
