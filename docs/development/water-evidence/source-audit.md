# Water and hydrology: source contract audit and bounded coverage (WA01, #2587)

Parent: #2582 (Climate and Environment, subdomain `water-hydrology`, ADR-005).
This audit records, per source, the endpoints, authentication and key
handling, licence and redistribution terms, rate limits, and how updates,
corrections and removals are identified, and selects the bounded coverage of
the `climate-environment-water` source pack
(`packs/climate-environment/source_packs/climate-environment-water.json`,
shipped by the Climate and Environment bundle for its optional `water`
feature). The machine-readable copy of each decision is `PROVIDER_CONTRACTS`,
`NOT_IMPLEMENTED`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION` in
`src/ingestion/water_sources.py` and `MINIMISATION` in
`src/kb/water_records.py` (served by the `water_source_contracts` MCP tool).

**Terms were not re-verified live.** The public documentation and terms pages
(`pegelonline.wsv.de`, `api.waterdata.usgs.gov`, `eea.europa.eu`,
`grdc.bafg.de`) were unreachable from this runtime (egress blocked), so this
audit is written from the tracker's references and prior knowledge of the
services. Every item marked *verify* must be checked during the live
validation issue (WA13, #2647) and recorded in [`README.md`](README.md) beside
this file. Every provider is `unverified-live`.

## Why a separate source pack

As for biodiversity, the water sources ship as their own source pack inside
the Climate and Environment bundle rather than as new entries of
`climate-environment.json` (the tracker's first suggestion): the feature is
optional and off by default, so the bundle's own `climate-environment` ^1.0.0
pin, its sources, fixtures and hashes stay byte-identical, and only the
`environment.water` provider pins `climate-environment-water` ^1.0.0. Each
source carries its `LIVE_VERIFICATION` status in its `water` block.

## Scope boundary (all sources)

- No flood forecasting and no forecast values.
- No interpolation, resampling or gap filling: a value the source did not
  publish stays missing, and answers say so.
- No own water-body status assessment: status is the reporting authority's per
  reporting cycle, and cycles are never merged into one status.
- No flood-risk scoring; thresholds are only the characteristic values the
  gauge operator publishes, quoted as published.

## Per-source decisions

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Updates, corrections, removals | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| PEGELONLINE (WSV) | HTTPS GET `/webservices/rest-api/v2/stations/{uuid}.json?includeTimeseries=true&includeCharacteristicValues=true`; `/stations/{uuid}/{W or Q}/measurements.json?start&end` | None | Datenlizenz Deutschland Zero 2.0 (*verify*); redistribution unrestricted; attribution "WSV, PEGELONLINE" kept for citation | No published hard limit (*verify*); one station request plus one per declared series | Raw, unchecked data (Rohdaten), kept about 31 days; no approval state and no revision marker; station metadata (location, gauge zero with `validFrom`, characteristic values) updated in place, so changes are detected between retrievals and stored as revisions. Values rolling off the 31-day window are **not** removals: PEGELONLINE windows are never compared for absence | Selected, `unverified-live` |
| USGS Water Data APIs (OGC API) | HTTPS GET `/ogcapi/v0/collections/monitoring-locations/items/{id}?f=json`; `/ogcapi/v0/collections/daily/items?monitoring_location_id&parameter_code&statistic_id&time&limit&f=json` (*verify* field names; the legacy `waterservices.usgs.gov` is being retired) | Optional api.data.gov key as the `NOESIS_USGS_WATER_API_KEY` secret reference, sent as `X-Api-Key` (*verify*); never stored in manifests, receipts or records; without it runs use the anonymous limit and the receipt says `not configured` | U.S. public domain (USGS data policy); cite USGS; provisional-data disclaimer kept | api.data.gov hourly limits per key or IP (*verify* numbers); one location request and one daily page (at most 100 values) per selection; HTTP 429 is receipted as `rate_limited` | `approval_status` (Provisional/Approved) and qualifiers per value, `last_modified` per value; provisional values may be revised or deleted before approval: a change is a new revision, and a value absent from a later complete window of the same selection gets a dated tombstone revision | Selected, `unverified-live` |
| EEA WISE WFD (Discodata) | HTTPS GET `/sql?query=<bounded SELECT>&p=1&nrOfHits=n` over the WISE WFD surface-water-body status view and the water-body geometry view (*verify* view names; the geometry may instead come from the WISE spatial service on a second host) | None | EEA re-use policy, CC BY 4.0 unless stated (*verify* per dataset); data reported by Member States; attribute the EEA and the reporting State | No published hard limit (*verify*); `nrOfHits` bounds each page; one geometry and one status page per selection | One dataset per WFD reporting cycle (2016, 2022); resubmissions replace rows in the `latest` views without a row-level marker: a changed row is a new revision of that cycle's record; a withdrawn or not yet reported code is listed in the receipt (`unreported_codes`) | Selected, `unverified-live` |
| GRDC (BfG) | Data on request after registration | Registration | Data for the requester's own use; redistribution and publication not permitted (*verify*) | n/a | n/a | **Not implemented** (`NOT_IMPLEMENTED["grdc"]`): records could not be cited or exported |

## Data minimisation

Decision: **no personal data** (`MINIMISATION` in `src/kb/water_records.py`).

- Stored: station, observation, water-body and assessment facts as published
  by the gauge operator or reporting authority; organisational names only
  (agency, competent authority, river basin district).
- Excluded: contact persons, e-mail addresses, telephone numbers, observers,
  field personnel and addresses (`PERSONAL_KEYS`). None of the selected fields
  carries personal data; any personal-looking key in a payload is dropped and
  listed in the page receipt (`personal_fields_dropped`).
- Redacted: nothing needs redaction.
- Retention: revisions are kept with their record; removals by a source are
  tombstone revisions.
- Who may query: principals holding `knowledge:environment:read` and namespace
  access.
- Enforcement: `statement()` rejects personal keys at write time (tested), and
  MCP answers are built from stored records only.

## Bounded coverage

| Axis | Selection | Why |
| --- | --- | --- |
| PEGELONLINE | Two declared stations on one water (UUID and number); series W and Q; one 2-hour window per series (at most 31 days) | Exercises gauge zero with validity, characteristic values, a gap and both parameters |
| USGS | Two monitoring locations; daily mean discharge (00060) and gage height (00065), one week each; at most 100 values per page | Exercises provisional to approved revisions, qualifiers, a missing day and a withdrawn provisional value |
| EEA WISE | Three surface water bodies in one country, cycles 2016 and 2022; one geometry vintage where published | Exercises cycles side by side, an unknown status, potential for a heavily modified body and a body without a geometry |
| Caps | 3 series per station, 31-day windows, 20 water bodies per selection, 10 pages per source | Nothing is enumerated; every refresh stays inside the declared selection |

## Live verification

Every provider has a `LIVE_VERIFICATION` entry with its intended status. All
are `unverified-live` until WA13 (#2647) records a dated bounded run; GRDC is
`not-implemented`; the optional USGS key is not configured here.
