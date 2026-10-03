# Oceans and marine environment: source-contract audit and bounded coverage (OM01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

This audit sets out, per source, what the Climate and Environment bundle's
proposed `environment.marine` provider (subdomain `oceans-marine`, ADR-005)
may acquire, how, and on what terms. No per-track tracker or delivery issue
exists yet; they are opened once this audit names a surviving source, which it
does (see the access decisions). The live-validation issue is opened with
them.

**It was written without network access: the publisher hosts
(`coastwatch.pfeg.noaa.gov`, `erddap.ifremer.fr`, `data-argo.ifremer.fr`,
`argovis-api.colorado.edu`, `data.marine.copernicus.eu`,
`www.protectedplanet.net`, `discodata.eea.europa.eu`) are blocked by this
runtime's egress proxy (verified 2026-10-03), so terms, endpoints, dataset ids
and field names were not re-verified live.** They come from the gap table's
candidates and the publishers' documentation as the author knows it. Every
item marked _verify_ must be checked against the live pages, the live terms and
a real response before the first dated live run. No source is `live` until
that run exists.

**No machine-readable copy exists yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and `LIVE_VERIFICATION` will be added
in `src/ingestion/marine_sources.py`, and `MINIMISATION` in
`src/kb/marine_records.py`, by the track's acquisition issues. They must match
this audit; a difference is a defect in the code or a reason to amend this
audit first.

## Home and source pack

As for water and biodiversity, the marine sources are planned as their own
source pack (`climate-environment-marine`, proposed) inside
`packs/climate-environment/source_packs/`, pinned only by `environment.marine`.
The feature is optional and off by default, so the bundle's existing pins,
fixtures and hashes stay byte-identical. Taxonomy shapes: `observations`
(gridded and profile values) and `registry-records` (protected-site entries),
with site and box geometry through `places-geometry` in the geospatial store.

## Scope boundary (all sources)

- No ocean modelling, forecasting, regridding, interpolation or gap filling. A
  grid cell or pressure level the source did not publish stays missing.
- No anomalies, climatologies, trends or marine-heatwave detection computed by
  Noesis.
- No quality control of our own: QC flags, data modes and adjusted values are
  stored as published, and answers state them.
- No protected-area effectiveness, management or ecological-status judgement;
  site attributes are quoted as the reporting authority published them.

## Access decisions

| Source | Publisher | Delivers | Decision |
| --- | --- | --- | --- |
| ERDDAP griddap, NOAA OISST v2.1 | NOAA (CoastWatch West Coast node serving NOAA NCEI OISST) | daily sea-surface temperature on a 0.25 degree grid, a published analysis | selected, `unverified-live` |
| Argo profiles via ERDDAP tabledap | Argo programme (GDACs at Ifremer and US GODAE), served by Ifremer ERDDAP | float profiles of pressure, temperature and salinity with data mode and QC flags | selected, `unverified-live` |
| Argo GDAC files and Argovis API | Argo GDAC; Argovis (University of Colorado) | per-profile NetCDF files; a JSON re-serving of GDAC | `not-implemented` in first coverage (reason below) |
| Copernicus Marine Service | Mercator Ocean International for the EU | reanalyses, analyses and in-situ products, subset by bbox, time and variable | `gated-not-granted` |
| WDPA marine areas (Protected Planet) | UNEP-WCMC and IUCN | protected-area attributes and polygons | `not-implemented` (terms) |
| Natura 2000 marine sites (EEA) | EEA, reported by Member States | site register entries with marine share and designation | selected as the protected-area substitute, `unverified-live` |

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| ERDDAP OISST | `https://coastwatch.pfeg.noaa.gov/erddap/info/{datasetID}/index.json` (metadata) and `/erddap/griddap/{datasetID}.json?sst[(t0):1:(t1)][(0.0)][(lat0):1:(lat1)][(lon0):1:(lon1)]`; dataset id `ncdcOisst21Agg_LonPM180` and the `zlev` axis _verify_ | none | NOAA data, U.S. public domain; the dataset's own `license` attribute is recorded per retrieval _verify_; cite NOAA NCEI OISST v2.1 with its DOI _verify_ | no published quota; ERDDAP answers overload with HTTP 503 or 429 _verify_; one metadata and one data request per selection | OISST publishes a **preliminary** field first and replaces it with a **final** field about two weeks later _verify_; the state comes from the dataset attributes or the aggregation's file names _verify_ and maps to `provisional`/`final`; a replaced value is a new revision; `date_modified`/`history` attributes date each retrieval's vintage. OISST is itself an optimal-interpolation analysis and is labelled `analysis`, never `in-situ` |
| Argo via Ifremer ERDDAP | `https://erddap.ifremer.fr/erddap/tabledap/ArgoFloats.json?platform_number,cycle_number,direction,data_mode,time,latitude,longitude,position_qc,pres,pres_qc,pres_adjusted,temp,temp_qc,temp_adjusted,psal,psal_qc,psal_adjusted&time>=..&time<=..&latitude>=..&...` (dataset id and variable names _verify_) | none | Argo data are free and unrestricted with citation of the Argo DOI (10.17882/42182) and the GDAC snapshot used _verify_; CC BY 4.0 _verify_ | as ERDDAP above; one page per selection | `data_mode` per profile: `R` real-time, `A` real-time adjusted, `D` delayed mode. Delayed-mode QC replaces real-time profiles months later _verify_; each value's raw and `_adjusted` field and its QC flag (`1` good to `4` bad, `9` missing) are kept as published; an R-to-D change or a changed flag is a new revision. The ERDDAP copy may lag the GDAC _verify_; the GDAC stays the authority and the retrieval records the ERDDAP `date_modified` |
| Argo GDAC and Argovis | GDAC: `https://data-argo.ifremer.fr/dac/{dac}/{wmo}/profiles/{R or D}{wmo}_{cycle}.nc`, index `ar_index_global_prof.txt` _verify_; Argovis v2 JSON API _verify_ | GDAC none; Argovis an API key after free registration (`NOESIS_ARGOVIS_API_KEY` if ever used) _verify_ | as Argo above | GDAC none published; Argovis per-key limits _verify_ | monthly GDAC snapshots with DOIs _verify_. **Not implemented in first coverage:** the global index is too large to fetch inside the caps and NetCDF parsing is a new dependency; Argovis duplicates GDAC behind a key. Both are candidates for a later version |
| Copernicus Marine | Copernicus Marine Toolbox subset and ARCO services (`data.marine.copernicus.eu`, `s3.waw3-1.cloudferro.com`) _verify_ | account after registration: `NOESIS_COPERNICUS_MARINE_USERNAME` and `NOESIS_COPERNICUS_MARINE_PASSWORD` as secret references, never in manifests, receipts or records | Copernicus Marine Service licence: free and open use with attribution and the product DOI; redistribution with attribution _verify_ | per-account fair use _verify_ | products carry dataset versions (for example a `_202311` suffix) and separate near-real-time, interim and multi-year (reprocessed) streams _verify_; a new dataset version is a new series, never spliced. **Gated, not granted:** no account or licence acceptance exists for this deployment, and the toolbox is a new dependency; off by default, as `copernicus-cams` in `src/kb/environment_places.py` |
| WDPA | Protected Planet downloads and API v3 (`api.protectedplanet.net/v3`, token on request) _verify_ | API token by request | WDPA Terms of Use: non-commercial use; no redistribution of the data or substantial extracts to third parties; commercial use through IBAT _verify_ | n/a | monthly releases (`WDPA_<Mon><Year>`) _verify_. **Not implemented:** cited evidence-bundle export would redistribute WDPA content, and Noesis deployments cannot be assumed non-commercial. Kept as a documented contract so queries report it as unavailable, not empty |
| Natura 2000 (EEA) | EEA Discodata bounded SQL over the Natura 2000 tabular release, `https://discodata.eea.europa.eu/sql?query=<SELECT>&p=1&nrOfHits=n` (view names `[N2K]...` _verify_); geometry deferred | none | EEA re-use policy, CC BY 4.0 unless stated _verify_; attribute the EEA and the reporting State | no published quota _verify_; `nrOfHits` bounds each page | one release per reporting year ("end 20xx") _verify_; each release is a vintage; a changed row is a new revision; a site absent from a later complete release of the same declared selection gets a dated tombstone revision |

**Unavailable-access fallback.** A failed request (HTTP error, redirect to
another host, schema drift, a response over the byte cap) fails that source's
run with its code and a receipt; earlier revisions stay current, nothing is
marked removed, and readiness reports the source as `stale`.

## Record shape and reuse

- **Observations:** one value per variable, grid cell or pressure level, and
  time, with unit, quality state and flags as published, following the
  statement pattern of `src/kb/water_records.py` (quality states, `unknowns`,
  `FORBIDDEN_KEYS` and `PERSONAL_KEYS`) on the scopes and digests of
  `src/kb/environment_records.py`. The revision store follows
  `src/kb/water_store.py`; vintage comparison reuses
  `src/kb/environment_vintages.py`.
- **Registry records:** Natura 2000 site entries keyed by the EU site code.
- **Places:** declared boxes and site codes are places in `src/kb/geospatial.py`;
  no geometry is guessed from names.
- **Adapters:** the Discodata path reuses the bounded SELECT pattern of
  `wise_sql` in `src/ingestion/water_sources.py`; ERDDAP needs a small new
  griddap/tabledap adapter whose byte budget follows `FeatureBudget` in
  `src/ingestion/geojson_features.py`.
- **Links (by citation only):** fisheries through FAO major area 27 places
  (`src/kb/fisheries_identity.py`); climate through `environment.core`. Absent
  providers degrade to an unavailable report; no proximity or causal links.

## Data minimisation

Environmental measurements; no source is about people. Decision:

- **Stored:** values, flags, data modes, dataset and site metadata, and
  organisational names (operating institution, DAC, reporting authority).
- **Excluded:** Argo `PI_NAME` and any principal-investigator field; ERDDAP
  `creator_name`, `creator_email`, `contributor_name` and similar person
  attributes; Natura 2000 site-manager contacts and addresses. Dropped keys are
  listed in the page receipt and rejected at write time.
- **Also excluded:** Natura 2000 species tables (biodiversity's concern, with
  sensitive-species rules in `src/ingestion/biodiversity_sources.py`).
- **Retention:** revisions are kept with their record; removals by a source are
  tombstone revisions.
- **Who may query:** principals with `knowledge:environment:read` and namespace
  access.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| ERDDAP OISST | German Bight box 53.5-55.0 N, 6.5-9.0 E; variable `sst`; one declared 7-day window | 1 variable, 7 days, 2 000 values and 1 MB per response |
| Argo (ERDDAP) | Baltic Proper box 55.5-58.5 N, 17.0-21.0 E (Gotland Basin); one declared 90-day window; core parameters only | 50 profiles, 200 levels per profile, 5 MB per response |
| Natura 2000 | three declared marine site codes in the German North Sea EEZ; two consecutive releases | 10 sites, 2 releases, 5 pages |
| All | nothing enumerated; every refresh stays inside the declared selection | 10 requests per source per run |

Justification: the German Bight is the smallest box that overlaps the
North Sea Natura 2000 sites and FAO area 27, and its 7-day window is enough to
see a preliminary value replaced by a final one. Argo floats rarely profile the
shallow German North Sea, so the Argo box sits where floats operate in the
Baltic; an empty page is reported as empty for the selection, never as
absence. Each further box, variable or product is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| ERDDAP OISST | `unverified-live` | - | none yet |
| Argo via ERDDAP | `unverified-live` | - | none yet |
| Argo GDAC and Argovis | `not-implemented` | - | not in first coverage (index size, NetCDF dependency, Argovis key) |
| Copernicus Marine | `gated-not-granted` | - | no account or licence acceptance; credentials not configured |
| WDPA | `not-implemented` | - | non-commercial and no-redistribution terms _verify_ |
| Natura 2000 (EEA) | `unverified-live` | - | none yet |

The fixtures will be authored, not captured: synthetic values for dates in
2094-2097 and releases dated 2098-2099, so nothing can be mistaken for a
published measurement.
