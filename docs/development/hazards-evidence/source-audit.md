# Natural Hazards source audit and bounded coverage (NH01, #2304)

Parent: #2207. This audit records, for each initial source, the endpoint and format, licence and
attribution, rate limits, how revisions or advisory numbers are identified, and the access decision.
It fixes the bounded first coverage the adapters in `src/ingestion/hazard_sources.py` implement and the
source pack `config/source_packs/natural-hazards.json` declares.

Every source is **`unverified-live`** (`LIVE_VERIFICATION` in `src/ingestion/hazard_sources.py`) until
a dated live run is recorded under this directory (NH15, #2375). Statements marked *verify* come from
the providers' public documentation and must be checked against live terms and responses before any
live run. Offline evidence is authored fixtures only (`tests/fixtures/hazards/`, all values fictional,
dated 2099) and is reported separately from live evidence.

Out of scope for every source: hazard prediction or forecasting by Noesis, risk scores, damage or loss
estimation beyond quoting the publisher, evacuation, safety or protective-action advice, and
attribution of events to climate change or any other cause.

## Summary

| Source | Provider key | Access decision | Auth | Records produced | Revision marker |
| --- | --- | --- | --- | --- | --- |
| USGS earthquake catalog (FDSN event) | `usgs` | unverified-live | none | `hazard_event` (earthquake) | event `updated` (epoch ms) |
| USGS PAGER (ComCat `losspager` product) | `usgs` | unverified-live | none | `impact_estimate` | product `updateTime` (+ `code`) |
| EMSC FDSN event service | `emsc` | unverified-live | none | `hazard_event` (earthquake) | `lastupdate` |
| GDACS event API | `gdacs` | unverified-live | none | `hazard_event`, `alert`, `impact_estimate` | `episodeid` (+ `datemodified`) |
| NOAA NHC advisories | `nhc` | unverified-live | none | `advisory`, `hazard_event` (tropical cyclone) | advisory number (`12`, `12A`) |
| Copernicus EFFIS burnt areas | `effis` | unverified-live | none | `hazard_event` (wildfire) | `LASTUPDATE` |
| EFFIS active fires (hotspots) | — | **not implemented** | — | — | — |
| Copernicus GloFAS notifications | `glofas` | **key-gated, disabled by default** | `NOESIS_GLOFAS_TOKEN` | `alert` (flood, modelled) | notification issue time |

## USGS earthquake catalog — FDSN event service

- **Endpoint / format.** `https://earthquake.usgs.gov/fdsnws/event/1/query` with `format=geojson`,
  `starttime`, `endtime`, `minlatitude`/`maxlatitude`/`minlongitude`/`maxlongitude`,
  `minmagnitude`, `orderby=time`, `limit` and `includedeleted=true`. Each feature's `id` is the
  preferred event ID; `properties.ids` lists every associated network ID (`,us7000…,at00…,`);
  `properties.status` is `automatic`, `reviewed` or `deleted`; `properties.updated` is the
  publisher's last-update time in epoch milliseconds; `mag`/`magType`, `geometry.coordinates`
  (`[lon, lat, depth_km]`), `time`, `place`, `net`, `sources`, `alert` (PAGER level) and `detail`.
- **Revisions.** The service returns the *current* preferred origin only. Each acquisition whose
  `updated` differs is appended as a new revision keyed by `updated`; magnitude type and value,
  location, depth and review status are stored per revision. Earlier origins are not recoverable
  from the summary service (they exist as older ComCat product versions — *verify* before relying on
  them) so the revision history is the history Noesis observed, labelled with the publisher's time.
- **Deleted / merged.** `includedeleted=true` returns deleted events with `status=deleted`; they
  become a revision with that status, never a removal. An event requested by an ID that is now a
  secondary ID of another preferred event is recorded as `merged` into that preferred ID (the
  secondary ID appears in the preferred event's `ids`).
- **Licence / attribution.** USGS-authored data are U.S. public domain; credit "U.S. Geological
  Survey" is requested (https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits).
  Contributed network data keep their contributors' credit (`sources`).
- **Rate limits.** No published request quota; a query may return at most 20,000 events
  (*verify*). The adapter sends one bounded query per declared window.

### PAGER (ComCat `losspager` product)

- **Endpoint / format.** The event detail GeoJSON (`…/query?eventid=<id>&format=geojson`) lists
  `properties.products.losspager[]` with `code`, `source`, `updateTime`, `status` and
  `properties.alertlevel` (plus `maxmmi` and other published fields, *verify*).
- **Versions.** A PAGER product version is identified by `code` + `updateTime`; every version is
  stored as an `impact_estimate` citing that version. The alert level is quoted; losses are never
  estimated or restated as a Noesis finding.

## EMSC — FDSN event service

- **Endpoint / format.** `https://www.seismicportal.eu/fdsnws/event/1/query` with `format=json`
  (GeoJSON features: `properties.unid`, `time`, `lastupdate`, `mag`, `magtype`, `lat`, `lon`,
  `depth`, `auth`, `flynn_region`, `evtype`, `source_id`, `source_catalog`; *verify* field names).
- **Revisions.** Keyed by the EMSC `unid`; each `lastupdate` is a revision storing magnitude,
  location, depth and the authoring agency (`auth`) as published.
- **Separation.** EMSC and USGS parameters for the same earthquake are separate records. They are
  only ever linked by a reviewed correspondence (NH08) and never merged or averaged.
- **Felt reports / testimonies.** Excluded: they are personal testimonies and not needed for the
  published event parameters.
- **Licence / attribution.** EMSC data may be reused with acknowledgement of the EMSC-CSEM and the
  contributing agencies; commercial redistribution needs the EMSC's agreement (*verify*
  https://www.emsc-csem.org/). Noesis quotes and cites; it does not redistribute bulk data.
- **Rate limits.** None published (*verify*); one bounded query per declared window.

## GDACS — event API

- **Endpoint / format.** `https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH` with
  `eventlist` (EQ, TC, FL, VO, DR, WF), `fromdate`, `todate` and optional `alertlevel`
  (GeoJSON features: `eventtype`, `eventid`, `episodeid`, `glide`, `name`, `alertlevel`,
  `alertscore`, `episodealertlevel`, `episodealertscore`, `severitydata.severitytext`,
  `affectedcountries[]`, `fromdate`, `todate`, `datemodified`, `url.report`; *verify*).
- **Revisions.** Keyed by event type + event ID; each alert episode (`episodeid`) is a revision of
  the event and an `alert` record (level, severity text, affected countries and episode time as
  published). The episode alert score is an `impact_estimate` quoting GDACS; it is never recomputed
  or turned into a Noesis risk score. GLIDE numbers are stored when published.
- **Licence / attribution.** GDACS is a cooperation framework of the UN and the European
  Commission; content may be used with attribution "GDACS" (*verify* https://www.gdacs.org/About/termsofuse.aspx).
- **Rate limits.** None published (*verify*); one bounded query per declared window.

## NOAA National Hurricane Center — advisories

- **Endpoint / format.** Forecast/advisory (TCM, `…fstadv.NNN.shtml`) and public advisory (TCP,
  including intermediate `NNNA` advisories, `…public.NNNa.shtml`) text products in the NHC archive
  (`https://www.nhc.noaa.gov/archive/<year>/`); `https://www.nhc.noaa.gov/CurrentStorms.json` lists
  active storms. GIS forecast cones and ATCF data are published separately (shapefile/KMZ).
- **Advisory identity.** Storm ID (`AL052099`: basin, number, year) + advisory number; intermediate
  advisories keep their letter suffix (`12A`). Each advisory is its own record exactly as issued:
  issue time, centre position, maximum sustained winds, gusts, minimum central pressure, forecast
  positions and watches/warnings text. The storm is a `hazard_event` revised by each advisory.
- **Forecast track and cone.** Forecast positions are stored as the NHC's published forecast with
  its issue time — never extended, interpolated or recomputed. The cone is referenced by locator
  (GIS product URL) and not mirrored.
- **Text.** Advisory text is referenced by locator (product URL); structured fields are parsed from
  the fixed-format product. A product that does not parse is rejected, never guessed.
- **Licence / attribution.** U.S. government work, public domain; credit "NOAA/NHC".
- **Rate limits.** None published (*verify*); one request per declared advisory product.

## Copernicus EFFIS — burnt areas

- **Endpoint / format.** EFFIS burnt-area polygons through the EFFIS OGC services
  (`https://maps.effis.emergency.copernicus.eu/effis`, WFS `GetFeature`, GeoJSON output; layer name
  and property names `id`, `FIREDATE`, `LASTUPDATE`, `AREA_HA`, `COUNTRY`, `PROVINCE`, `COMMUNE`
  are *verify*).
- **Revisions.** Keyed by the EFFIS burnt-area identifier; each `LASTUPDATE` is a revision storing
  the burnt-area estimate (hectares, as published), the mapping date and the published polygon.
  Burnt areas are satellite-mapped estimates published by EFFIS, labelled as such.
- **Active fires.** *Not implemented.* EFFIS active-fire layers are MODIS/VIIRS hotspot detections
  sourced from NASA FIRMS under NASA's terms; they are not EFFIS records and would need their own
  audit.
- **Licence / attribution.** Copernicus Emergency Management Service data: free and open with
  attribution "© European Union, Copernicus Emergency Management Service" (*verify*).
- **Rate limits.** None published (*verify*); one bounded `GetFeature` per declared selection.

## Copernicus GloFAS — flood notifications (access decision)

- **Access.** GloFAS forecast layers are served through the GloFAS portal and the Copernicus EWDS/CDS
  under the CEMS licence; flood notifications are distributed to registered partners. Both require
  registration and licence acceptance; layers may not be redistributed as mirrors.
- **Decision.** *Key-gated and disabled by default.* The source is declared with
  `auth.kind = required-secret` (`NOESIS_GLOFAS_TOKEN`) and is an optional bundle feature
  (`glofas`, default off). The adapter parses an operator-provided notification export (alert type,
  return-period thresholds, validity and issue time, river point) into `alert` records labelled as
  the publisher's modelled output with model version and issue time. No raster forecast layer is
  mirrored and Noesis makes no flood prediction. The fixture is authored in a declared shape (not a
  documented public API) and says so.

## Bounded first coverage

Chosen so that one physical event is visible in several publishers (to exercise NH08) while each
run stays small. Caps are enforced by the source-pack budgets (`max_results`, `max_pages`).

| Provider | Areas (lon/lat bbox) | Hazard types | Window per run | Record cap per run |
| --- | --- | --- | --- | --- |
| usgs | Eastern Mediterranean and Anatolia `[19, 34, 45, 43]` | earthquakes M ≥ 4.5 | 30 days | 200 events (+ PAGER for listed events) |
| emsc | same bbox | earthquakes M ≥ 4.5 | 30 days | 200 events |
| gdacs | same bbox and the North Atlantic `[-100, 5, -10, 45]` | EQ, TC, FL, VO, DR, WF | 30 days | 100 episodes |
| nhc | Atlantic basin (`AL` storms) | tropical cyclones | active storms | 10 storms × 60 advisories |
| effis | Greece, Türkiye, Italy, Spain, Portugal (`[-10, 34, 45, 46]`) | wildfire burnt areas ≥ 30 ha | fire season (declared) | 500 burnt areas |
| glofas | declared river points only (disabled by default) | flood notifications | 30 days | 50 notifications |

Justification: the Aegean/Anatolia box has frequent M4.5+ events reported by USGS, EMSC and GDACS
alike; the North Atlantic box covers NHC storms that GDACS also tracks; EFFIS burnt areas in the
same Mediterranean countries let a place (e.g. an island) show earthquakes and wildfires together.
Anything outside these boxes is **source not covered**, which answers distinguish from **none on
record** (`src/kb/hazards_queries.py`).

## Source-pack entries

`config/source_packs/natural-hazards.json` (pack `natural-hazards`, connector `natural-hazards`)
declares one source per provider with its licence, budget, `natural_hazards.live_verification`
(`unverified-live`, or `key-gated` for GloFAS) and a pinned authored fixture. The source pack is
consumed by `src/ingestion/source_pack_runtime.py`; pages are projected into
`src/kb/hazards_store.py` by the `noesis-hazard-record-v1` projector.
