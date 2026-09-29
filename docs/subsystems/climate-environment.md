# Climate and Environment

Status: implemented offline, 2026-09-27 (tracker [#1849](https://github.com/Ikey168/Noesis/issues/1849)).
Live provider coverage is **not** validated: the dated live check could not
reach any provider (see [live evidence](#live-and-offline-evidence)).

Given a place, the bundle assembles a source-cited environmental dossier:
air-quality and weather observation series with their stations and units,
model (reanalysis) and forecast series for the place's grid cell, grid
generation/load/unavailability records for its bidding zone, nearby industrial
facilities with releases, permits, EU ETS records and operator identity state,
and Berlin Umweltatlas layers containing the place. Every item carries its
source, kind (`observation`, `model`, `forecast`) and as-of basis.

It replaces the retired `energy` example pack, whose outage panel was keyword
matching; grid unavailability is now a published record, never inferred from
news.

**Never:** an attribution claim, a climate projection, a compliance
determination, a forecast or reanalysis value presented as an observation, an
outage inferred from news, or a legal threshold asserted by the pack.

| Module | Owns |
| --- | --- |
| `src/ingestion/environment_providers.py` | access contracts, hosts, live state, request plans, fail-closed parsers, the `environment` source-pack connector and the DurableHTTP client |
| `src/kb/environment_records.py` | `noesis-environment-record-v1` constructors, mandatory kind, pint normalisation, schema registration |
| `src/kb/environment_store.py` | revisioned records, vintages and values, station/installation links, provider state, the source-pack projector |
| `src/kb/environment_places.py` | place dossiers (E08): spatial answers, as-of pins, replay, export, coverage gaps |
| `src/kb/environment_vintages.py` | vintage comparison and view pins (E09) |
| `src/kb/environment_identity.py` | operator identity decisions and obligation evidence (E10) |
| `src/kb/environment_monitoring.py` | place monitors over knowledge subscriptions (E11) |
| `src/kb/environment_bundle.py` | declaration, enablement, readiness (E12) |
| `src/policy_monitor/environment_evidence.py` | read-only view of evidence attached to policy-monitor obligations |
| `tools/knowledge_engine_mcp/environment.py` | 21 MCP tools |

## Provider audit and access decisions (E01)

`PROVIDER_CONTRACTS` records, per source: documented access path, terms,
authentication, rate limits, pagination, cadence, retained evidence, what it
publishes (observation, model output, forecast or map features), identifiers,
units, CRS, bounded coverage and the unavailable-access fallback (record the
failure code, keep stored revisions, mark dependent views stale, never infer
values). `environment_provider_contracts` returns them with `LIVE_VERIFICATION`.

| Source | Access used | Auth | Publishes | Identifiers / units / CRS | Live (2026-09-27) |
| --- | --- | --- | --- | --- | --- |
| OpenAQ | REST v3 `/locations/{id}`, `/sensors/{id}/hours` | API key header (`NOESIS_OPENAQ_API_KEY`) | observation (hourly aggregates) | location/sensor id, EEA code in name; µg/m³; WGS84 | blocked, credential missing |
| Umweltbundesamt | Luftdaten API v3 JSON | none | observation; current-year data provisional | UBA id + EEA code; component units; WGS84; timestamps CET | blocked |
| ENTSO-E | Transparency REST (XML; A80 zipped) | `securityToken` (`NOESIS_ENTSOE_SECURITY_TOKEN`) | observation (A16), forecast (A01, issue time = createdDateTime) | EIC zones, unit mRID, document mRID + revision; MW | blocked, credential missing |
| SMARD | `chart_data` weekly JSON files | none | observation (410, 4068, 4067), forecast (411) | filter/region/resolution/timestamp; MWh per interval | blocked |
| EEA Industrial Emissions | Discodata SQL JSON (pinned query) | none | observation (reported releases, method M/C/E) | INSPIRE id, ETS identifier; kg; WGS84 | blocked; table names unverified |
| EU ETS Union Registry | pinned export file read as CSV | none | observation (verified emissions, allocations, compliance code as published) | registry + installation id; tCO2e, allowances (not converted); no coordinates | blocked; XLSX parsing not implemented |
| Berlin Umweltatlas | WFS 2.0.0 via `wfs_api.py` (geospatial-berlin 1.2.0) | none | map features (computed/modelled maps are labelled) | WFS feature id; EPSG:25833 → WGS84 | blocked; service names unverified |
| Open-Meteo archive | Historical Weather API | none | **model** (ERA5 reanalysis) | grid cell + model; published units; 0.25° | blocked |
| Open-Meteo forecast | Forecast API + model `meta.json` | none | **forecast** with run time | grid cell + model; ICON-D2 ~2.2 km | blocked |
| DWD | CDC open data files | none | observation; `recent` provisional, `historical` validated | 5-digit station id; °C, %; WGS84; UTC | blocked |
| Copernicus/CAMS | — | ADS account + key + licence | would be model/forecast only | — | **not implemented**: credentials and terms unverified; no keyless or scraped access |

## Records (E02)

`noesis-environment-record-v1` (JSON schema under `contracts/schemas/jsonschema/`,
registered through `register_environment_schemas` in the schema registry with
owner `environment.core`) has five record types: station, observation series,
facility/installation (activities, permits, releases), grid event (generation,
load, planned/unplanned unavailability) and indicator vintage.

- **Kind is mandatory and never defaulted.** `PROVIDER_KINDS` states what each
  dataset publishes; a record whose kind the dataset does not publish is
  rejected (`kind_not_published`), so ERA5 and ICON-D2 values can never become
  observations. A stored series never changes kind (`kind_conflict`). Forecasts
  keep an `issue_time` slot; model/forecast series name model, dataset, version
  and grid resolution.
- **Units.** Values are exact decimal strings with the published unit;
  `UNIT_EXPRESSIONS` maps unit text to pint and `normalise` converts through
  `src/integrations/units.convert_physical` (a per-unit-pair affine map
  calibrated from two receipts, digest kept). Unmapped units (e.g. ETS
  allowances) stay as published and are listed in `unknowns`.
- **Places.** Stations, facilities and grid cells become `geospatial_places`
  with point geometries through `GeospatialStore`, and stations/facilities are
  projected as features (`environment-stations`, `environment-facilities`)
  through `GeospatialFeatureStore`. No spatial table is added.

## Acquisition (E03–E07)

Each selection (explicit stations, zones, facilities or grid points) compiles
to a bounded request plan. The same parsers serve the source-pack runtime (the
`environment` connector: one page per planned request, cursor = position plus
a small carry, runtime budgets, receipts, quarantine and replay) and the
DurableHTTP client (`acquire_environment_source`: explicit budget, exact hosts,
no redirects, durable replayable receipts, credentials only in secret slots).

- **E03** OpenAQ and UBA stations become places with provider identifiers;
  measurements are observation series with pollutant, unit, averaging period
  and status basis. Stations published by both providers are linked on the
  shared EEA station code within 250 m (`same-station`), never merged.
- **E04** ENTSO-E and SMARD deliver generation per production type, actual and
  forecast load and unavailability with bidding zone, resolution and kind.
  Unavailability keeps unit, nominal capacity, start/end, business type and
  reason text exactly as published; an acknowledgement "no matching data" is a
  coverage note, not an outage.
- **E05** EEA facilities keep geometry, E-PRTR activity, permits and annual
  releases (unit, year, method). ETS installations become facilities (no
  published coordinates) linked to an EEA facility only through a published
  ETS identifier (`same-installation`); verified emissions and allocations are
  annual indicator series whose values carry the compliance code as published.
- **E06** Umweltatlas layers (low-emission zone, strategic noise-map bands,
  climate functions) are three WFS sources in the `geospatial-berlin` 1.2.0
  upgrade (`packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json`),
  applied through `preview_source_pack_upgrade_impact` / `apply_source_pack_upgrade`.
  Existing sources are unchanged, 1.1.0 stays retained, and composition pins
  `^1.1.0` accept 1.2.0.
- **E07** DWD station products are observations (`recent` provisional,
  `historical` validated, QN per value); Open-Meteo archive is kind `model`
  (ERA5) and forecast is kind `forecast` with the model run time from
  `meta.json`.

## Place dossiers (E08)

`build_environment_place_dossier` resolves the place (ambiguous mentions are
returned, not guessed), finds the boundary feature containing it (default:
ALKIS districts), runs `GeospatialFeatureStore.within` over the station
collection, proximity relations for facilities and grid cells, and a
containment relation for the bidding zone (authored coarse outline in
`config/environment/bidding_zones.json`, precision declared). Records and
vintages valid at `as_of_ms` are pinned; `replay_environment_dossier`
recomputes every spatial receipt from pinned geometry and verifies the content
hash. Every empty section returns an explicit coverage gap; a place with no
coverage returns status `coverage_gap`.

## Vintages (E09)

Following the economic provider-vintage pattern, each re-published value set
is a new vintage with `release_at_ms`/`retrieved_at_ms` and the basis labels
`release_at_basis` (`provider_reported`, `caller_supplied`,
`provider_vintage_fallback`), `retrieved_at_basis`, `vintage_basis` and
`release_time_status`, plus `revision_of`. `compare_environment_vintages`
lists added/removed/revised/status-changed values with both vintages cited.
Dossiers and obligation evidence pin vintages and read as stale when a newer
one exists.

## Identity and obligations (E10)

`propose_environment_operator_links` proposes candidates from facility
operators to `canonical_entities` (alias resolution) and LEI records
(registration number or normalized legal name). A different principal with
`knowledge:environment:review` accepts or rejects each with a reason;
unmatched operators stay source strings. The Corporate Ownership bundle is not
a dependency.

`attach_environment_obligation_evidence` shows a facility release (series,
pinned vintage, period, pint conversion into the obligation's unit) beside a
public obligation the policy monitor records in `versioned_assertions`, citing
both. `determination` is always null; the monitor decides nothing new.

## Monitoring (E11)

`create_environment_monitor` is a knowledge subscription over a place: user
thresholds (optionally citing a source and quote; never asserted by the pack)
evaluated on observation values only, unavailability events in the zone,
facility permits/releases and series vintages within a radius. `run_environment_monitor`
evaluates only at a committed watermark (committed by source-pack runs and the
maintenance orchestrator); replaying a watermark creates no events. Each run
lists dossiers and obligation evidence that became stale. No scheduler is
added: the `climate-environment` source pack declares its schedule.

## Bundle and MCP (E12)

`packs/climate-environment/manifest.json` (`noesis-pack-composition-v1`) binds
`environment.core` (`providers/environment.core.json`) plus `geospatial.core`,
`geospatial.transit`, `market.lei`, `platform.source-runtime` and
`platform.subscriptions`; each store has one owner. Once composed,
`set_climate_environment_bundle_enabled` is a coordinator selection change
with an activation receipt; disabling blocks only the environment tools.

| Tool | Scopes |
| --- | --- |
| `environment_provider_contracts` | none |
| `climate_environment_bundle_status`, `lookup_environment_series`, `list_environment_grid_events`, `list_environment_facilities`, `compare_environment_vintages`, `inspect_environment_dossier`, `replay_environment_dossier`, `export_environment_dossier`, `list_environment_operator_links`, `inspect_environment_obligation_evidence` | `knowledge:environment:read` |
| `build_environment_place_dossier`, `propose_environment_operator_links`, `attach_environment_obligation_evidence` | `knowledge:environment:write` |
| `acquire_environment_source` | `knowledge:environment:write`, `knowledge:ingestion:execute` |
| `register_environment_schemas` | `knowledge:environment:write`, `knowledge:schema:register` |
| `review_environment_operator_link` | `knowledge:environment:review` |
| `create_environment_monitor`, `run_environment_monitor` | `knowledge:environment:write`, `knowledge:subscriptions:write` |
| `poll_environment_monitor` | `knowledge:environment:read`, `knowledge:subscriptions:read` |
| `set_climate_environment_bundle_enabled` | `operator` |

Store reads also require `namespace:<ns>:read` (writes `namespace:<ns>:write`);
dossiers and monitors are owner-scoped.

## Live and offline evidence

- **Offline:** `tests/unit/environment/` (unit suites per module) and
  `tests/unit/domains/test_climate_environment_acceptance.py` (one test per
  acceptance row) run against authored fixtures
  (`tests/fixtures/environment/README.md`). Receipts say `execution: injected`
  or `source-pack` with fixture adapters. The demo
  `docs/examples/environment-place-dossier-demo.md` is offline evidence.
- **Live:** `scripts/environment_live_check.py` →
  `docs/development/environment-evidence/live-check-2026-09-27.json`. Every
  host failed with `ConnectError` (egress policy); OpenAQ and ENTSO-E also had
  no credential. `LIVE_VERIFICATION` is `blocked` for every provider and
  `not implemented` for CAMS.

## Limitations

- Coverage is the explicit Berlin selection only; bounded station snapshots are
  partial, so `within` answers report `coverage_incomplete`.
- Bidding-zone membership uses an authored coarse outline, not an official
  geometry; places near borders need a manual check.
- EEA Discodata table/column names, the ETS export link and columns, the
  Umweltatlas service/type names, UBA's CET timestamp convention and SMARD
  filter ids are unverified until a live run.
- ETS XLSX exports and Copernicus/CAMS are not implemented.
- The `within` query uses current feature revisions; the dossier's as-of
  applies to environment records and vintages.
