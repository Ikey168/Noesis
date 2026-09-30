# Water and hydrology: source contract audit and bounded coverage (WA01, #2587)

Parent: #2582 (the `water-hydrology` track of the domain coverage program,
ADR-005; wave 1 tracker #2578). This audit records, per source, the access
method, endpoints, authentication and key handling, licence and
redistribution, rate limits, the revision model (how updates, corrections and
removals are identified), the personal-data decision, and the bounded first
coverage of the `climate-environment-water` source pack
(`packs/climate-environment/source_packs/climate-environment-water.json`,
shipped by the Climate and Environment bundle for the `environment.water`
provider). The machine-readable copy of each decision is `PROVIDER_CONTRACTS`,
`NOT_IMPLEMENTED`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION` in
`src/ingestion/water_sources.py` and `MINIMISATION` in `src/kb/water_records.py`
(served by the `water_source_contracts` MCP tool).

## How this audit was made, and what is unverified

The official pages were consulted on **2026-09-30**. From this runtime every
direct fetch of the provider hosts was refused by the network egress proxy
(`www.pegelonline.wsv.de`, `api.waterdata.usgs.gov`, `waterdata.usgs.gov`,
`www.eea.europa.eu`, `www.govdata.de`, `grdc.bafg.de`). The statements below
were therefore taken from search-engine extracts of the official pages listed
with each point, read on 2026-09-30; none was read directly. Every point is
**unverified by direct read** and every provider stays `unverified-live` until
the live validation (WA13, #2647) re-reads the pages, runs the bounded
selections and records dated evidence in [`README.md`](README.md). Request
paths and field names marked *verify* are authored from the documentation
extracts and must be checked in that run.

## Why a separate source pack

As for the biodiversity feature, the water sources ship as their own source
pack inside the Climate and Environment bundle rather than as new entries of
`climate-environment.json` (which WA01 names as the drafting location): the
three water features are optional and off by default, so the bundle's own
`climate-environment` ^1.0.0 pin, its sources, fixtures and hashes stay
byte-identical, and only the `environment.water` provider pins
`climate-environment-water` ^1.0.0.

## Scope boundary (all sources)

- No flood forecasting: PEGELONLINE forecast series and any forecast product
  are never requested; no record accepts a forecast field.
- No interpolation, resampling or gap filling: values keep the published
  timestamp and unit; a time without a published value is answered as missing.
- No own status assessment: WFD status is the member state's reported value
  per cycle; cycles are never merged.
- No flood-risk scoring: a monitor notice that a value is above a station's
  published characteristic value compares two published numbers and says so.
- No causal link between an observation and a hazard event: links are
  citations, published relations, shared identifiers or accepted matches.

## PEGELONLINE (WSV)

| Item | Finding |
| --- | --- |
| Publisher | Wasserstraßen- und Schifffahrtsverwaltung des Bundes (WSV), PEGELONLINE |
| Access | REST API v2 under `https://www.pegelonline.wsv.de/webservices/rest-api/v2`, JSON; resource-oriented (stations, time series, measurements). Source: <https://www.pegelonline.wsv.de/webservice/dokuRestapi> and <https://www.pegelonline.wsv.de/webservice/guideRestapi> (read via search extract, 2026-09-30) |
| Endpoints used | `/stations/{uuid}.json?includeTimeseries=true&includeCharacteristicValues=true`; `/stations/{uuid}/{timeseries}/measurements.json?start&end` (*verify* parameter names and ISO form) |
| Authentication | None: "no authentication or authorization is required" (same source) |
| Licence | DL-DE->Zero-2.0 (Datenlizenz Deutschland – Zero – 2.0). Source: terms of use <https://www.pegelonline.wsv.de/gast/nutzungsbedingungen> ("current as of 21 May 2024" in the extract) |
| Redistribution | Permitted without conditions under DL-DE Zero 2.0; Noesis still cites PEGELONLINE (WSV) and the request URL |
| Rate limits | None documented in the extracts (*unverified*); Noesis sends one request per declared station or window |
| Data quality | Current values are unchecked raw values delivered without warranty ("Rohdaten, ungeprüft"). Source: <https://pegelonline.wsv.de/gast/hilfe>, <https://pegelonline.wsv.de/gast/downloads>. Every PEGELONLINE value is stored with quality state `provisional` and the published wording |
| Availability | Measurements for the last 31 days through the API; older raw data only as downloads (not used). Source: dokuRestapi extract |
| Identifiers | Station `uuid` (stable key) and `number`; water `shortname`/`longname`; time series `shortname` (`W` level, `Q` discharge) |
| Datum | `gaugeZero` per time series with `unit` (metres above NHN) and `validFrom` (ISO date). Source: dokuRestapi extract |
| Revision model | A changed station (location, `gaugeZero` or its `validFrom`, characteristic values) is a new station revision; a measurement republished with another value is a new observation revision; a declared station no longer served (HTTP 404) gets a dated `removed` revision. Nothing is deleted |

## USGS Water Data APIs

| Item | Finding |
| --- | --- |
| Publisher | U.S. Geological Survey (USGS) |
| Access | OGC API – Features collections under `https://api.waterdata.usgs.gov/ogcapi/v0/`. Sources: <https://api.waterdata.usgs.gov/> (landing), <https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous>, <https://www.usgs.gov/tools/usgs-water-data-apis> (read via search extract, 2026-09-30) |
| Endpoints used | `monitoring-locations/items/{monitoring_location_id}`; `continuous/items?monitoring_location_id&parameter_code&time&limit&f=json`; `daily/items?…&statistic_id` (*verify*) |
| Identifiers | `monitoring_location_id` = agency code + site number (e.g. `USGS-02238500`); parameter codes (`00060` discharge, `00065` gage height); `time_series_id` |
| Authentication | Optional api.data.gov key sent as `X-Api-Key`; held as the secret reference `NOESIS_USGS_WATER_API_KEY`, never written to manifests, receipts or records; a response echoing the key is refused |
| Rate limits | Extracts report a low hourly limit per IP address without a key (50–100 requests/hour stated inconsistently) and 1,000 requests/hour per key, with `x-ratelimit-*` response headers. Source: <https://api.waterdata.usgs.gov/docs/ogcapi/efficiency/> and USGS blog <https://waterdata.usgs.gov/blog/api-whats-new-wdfn-apis/> (*unverified*); HTTP 429 is handled with `Retry-After` |
| Licence | USGS-authored data are U.S. Public Domain; credit "U.S. Geological Survey" requested. Source: <https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits> |
| Redistribution | Permitted (public domain) |
| Quality | Per value `approval_status` `Provisional` or `Approved`, and `qualifier` as a list (e.g. estimated, ice-affected), separate from approval status. Sources: dataRetrieval documentation <https://doi-usgs.github.io/dataRetrieval/articles/read_waterdata_functions.html> and the continuous collection page (search extract) |
| Revision model | Provisional values are revised or approved later; each published state of one value (location, parameter, statistic, time) is a revision of the same observation. Window truncation (a `next` link) is recorded in the receipt, not followed beyond the declared limit |

## EEA WISE Water Framework Directive database

| Item | Finding |
| --- | --- |
| Publisher | European Environment Agency (EEA) |
| Content | River Basin Management Plan data reported under WFD article 13: surface water bodies, category, ecological status or potential, chemical status, pressures and impacts, per reporting cycle. Source: <https://www.eea.europa.eu/en/datahub/datahubitem-view/dc1b1cdf-5fa0-4535-8c89-10cc051e00db> (search extract, 2026-09-30) |
| Access | EEA Discodata SQL service, database `[WISE_WFD]` (`[WISE_WFD].[latest]` named in the extract; a versioned `[v2r1]` schema also appears): `https://discodata.eea.europa.eu/sql?query=…&p=1&nrOfHits=N` (*verify* table and field names: `cYear`, `euSurfaceWaterBodyCode`, `swEcologicalStatusOrPotentialValue`, `swChemicalStatusValue`, …). Published water-body geometries from the WISE map service `https://water.discomap.eea.europa.eu/arcgis/rest/services/WISE_WFD/…/MapServer/{layer}/query?…&f=geojson` (*verify* service and layer). Source: <https://water.discomap.eea.europa.eu/arcgis/rest/services/WISE_WFD/WFD2016_SurfaceWaterBody_WM/MapServer> (search extract) |
| Authentication | None |
| Licence | CC BY 4.0 for WISE datasets (EEA re-use policy) per the extracts; *unverified by direct read* (terms page <https://www.eea.europa.eu/en/legal-notice>) |
| Redistribution | Permitted with attribution to the EEA |
| Rate limits | None documented in the extracts (*unverified*); one bounded query per declared selection |
| Identifiers | EU water-body code (`euSurfaceWaterBodyCode`) and reporting cycle year (`cYear`, e.g. 2016 for the 2nd RBMP, 2022 for the 3rd) |
| Revision model | One record per code and cycle; a republished row is a new revision; a declared code and cycle with no row after it was on record gets a dated `removed` revision; a new cycle is a new record. Status values are kept exactly as published |

## GRDC (Global Runoff Data Centre): not implemented

Terms (search extracts of <https://grdc.bafg.de/data/data_portal/> and
<https://grdc.bafg.de/data/data_portal_guide/>, 2026-09-30, unverified by
direct read): downloads need a request form and acceptance of the terms of
use, data protection regulation and data sharing conditions; "the
redistribution of the downloaded data either in part or total to unauthorized
persons, third parties or to the general public (including distribution via
Internet) is not allowed"; data are released for research purposes and
commercial use of the original data is not allowed. Noesis answers, exports and
shared deployments would redistribute values, so GRDC is recorded as
**not implemented** (`NOT_IMPLEMENTED["grdc"]`, `LIVE_VERIFICATION["grdc"] =
not-implemented`). No request is ever made to it.

## Personal data and minimisation

None of the selected fields names a natural person: stations and monitoring
locations belong to agencies (PEGELONLINE `agency` is a WSV office name), and
WFD rows describe water bodies. Decision (`MINIMISATION`):

- **Stored:** identifiers, names, agencies, locations, datums, characteristic
  values, observations with quality state and qualifiers, WFD status elements.
- **Excluded:** any personal field (`email`, `phone`, `contact`, `observer`,
  `owner_name`, … – `PERSONAL_KEYS`). Adapters drop such a field if a response
  carries it and name it in the page receipt; the record model rejects it at
  write time; every MCP answer passes through `minimise`.
- **Retention:** records are immutable revisions kept for provenance; there is
  no personal data to retain or expire.
- **Who may query:** `knowledge:environment:read` plus namespace access;
  identity review needs `knowledge:environment:review`.

## Bounded first coverage

| Source | Selection | Why |
| --- | --- | --- |
| PEGELONLINE | Two Elbe stations (Dresden and Meißen), their `W` series (and `Q` at Dresden), one-day windows; windows are refused above 31 days | Links to Natural Hazards (Elbe floods) and to a Berlin/Saxony place set already used by the geospatial fixtures; 31 days is the documented availability |
| USGS | One monitoring location (Potomac River at Little Falls, `USGS-01646500`), `00060` and `00065` continuous windows of one hour (limit 20) and a two-day daily mean window; windows are refused above 31 days or 1,000 values | Shows provisional-to-approved revisions and qualifiers on a well-known gauge within the public rate limit |
| EEA WISE | Two Elbe surface water bodies, cycles 2016 and 2022, one pinned table; their published geometries from one named dataset vintage | Shows per-cycle status history and geometry-based place mapping without merging cycles |

Record caps: `max_results` 500 and `max_pages` 20 per source run; selections
are explicit, and the offline fixtures use illustrative identifiers and values.
