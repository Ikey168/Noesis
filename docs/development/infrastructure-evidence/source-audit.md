# Critical infrastructure registries: source-contract audit and bounded coverage (CI01)

Tracking: #2223 · delivery issue #2359 · recorded 2026-09-29.

This audit decides, per source, what the Geospatial pack's optional
`infrastructure` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, parameters, column names and terms come from
the publishers' documentation as the author knows it. **Every item marked
_verify_ must be checked against the live documentation, the live terms and a
real response before the first dated live run (CI14, #2401). No source is
`live` until that run is recorded in [README.md](README.md).**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `EXCLUDED` and `LIVE_VERIFICATION` in
`src/ingestion/infrastructure_sources.py`. The source pack
`config/source_packs/geospatial-infrastructure.json`
(`geospatial-infrastructure` 1.0.0, domain `geospatial`) records each
selection beside its access decision, and the MCP tool
`infrastructure_source_contracts` returns them.

The feature extends the existing Geospatial pack. It adds no pack and no
spatial store: geometries go through `GeospatialStore.store_geometry`
(`src/kb/geospatial.py`) and containment through its spatial relations.
The Berlin source pack (`config/source_packs/geospatial.json`,
`geospatial-berlin`) is left untouched. Its 1.1.0 content is pinned by the
Climate and Environment (1.2.0) and housing (1.3.0) upgrade chains. The
infrastructure sources therefore ship as a separate `geospatial` domain
source pack.

## Security-sensitivity rule

Only attributes the publisher itself releases are stored, verbatim, with
the publisher's release and the retrieval time. The pack **never** enriches
an asset with sensitive detail. That means:

- no geocoding of assets whose publisher gives no coordinates;
- no inferring of routes, depths, protection or control systems;
- no joining with imagery;
- no scoring of criticality, vulnerability or dependency;
- no valuation.

OSM editor identities (`user`, `uid`) are dropped at parse time. Layers or
fields the publisher marks restricted (for example US Critical Energy/Electric
Infrastructure Information, CEII) are excluded and listed in `EXCLUDED`,
never requested.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| WRI Global Power Plant Database (GPPD) | Power plants: `gppd_idnr`, name, capacity (MW), primary and other fuels, commissioning year, owner, WEPP id, coordinates, reported and **estimated** generation | `acquire` (`unverified-live`) | A static, versioned release (v1.3.0, 2021) under CC BY 4.0. The CSV is fetched from the release tag of the WRI repository. The tag path is _verify_. Estimated generation columns are stored as estimates |
| Global Energy Monitor (GEM) trackers | Coal Plant Tracker units, Gas Infrastructure Tracker pipelines and LNG terminals: GEM ids, status, capacity, owner and parent with shares, start and retired year, coordinates or route WKT, wiki URL | `acquire` a **declared release file only** (`unverified-live`) | Tracker downloads are released through a request form, not an API. The operator declares the release file URL (on `globalenergymonitor.org`), label and date, and the pack parses that file. GEM states attribution terms. Some downloads carry non-commercial terms (_verify_ per tracker). Until the terms are verified, **every GEM record carries the non-commercial constraint**. Wiki notes are cited by URL and never copied |
| OpenStreetMap through Overpass | `power=plant/generator/line/substation`, `man_made=pipeline` and lifecycle-prefixed variants with tags, element version, changeset and timestamp | `acquire` bounded extracts (`unverified-live`) | ODbL 1.0. A database built from these records is a derivative database. If it is shared publicly, it must be offered under ODbL (share-alike) with the attribution "© OpenStreetMap contributors". Queries run against the public `overpass-api.de` instance with a fixed bbox, tag set, `timeout` and `maxsize` |
| US EIA energy infrastructure layers (U.S. Energy Atlas) | Power plants (plant code, name, operator, total MW, primary source), natural-gas pipelines (operator, type), LNG terminals (operator, capacity, status, FERC docket) | `acquire` (`unverified-live`) | U.S. government data, public domain. Cite the EIA. The layers are ArcGIS FeatureServer layers queried with `f=geojson`, a bbox and `resultRecordCount`. The service host, layer paths and field names are _verify_ |
| ENTSOG Transparency Platform | Interconnection and connection points (`pointKey`, label, type, operators) and firm technical capacity per point and direction (entry/exit) with unit | `acquire` (`unverified-live`) | Public REST API (`/api/v1/connectionpoints`, `/api/v1/operationaldatas`). ENTSOG transparency terms: reuse with attribution (_verify_). The API publishes no WGS84 coordinates for points, so **geometry stays unknown and is never geocoded by name**. The capacity map (PDF) and TSO-login data are excluded |

## Per-source contract

| Source | Access method | Licence and attribution | Redistribution | Rate limits and bounds | Release / as-of semantics |
| --- | --- | --- | --- | --- | --- |
| GPPD | one HTTPS GET of the release CSV (`raw.githubusercontent.com`, tag `v1.3.0` _verify_); rows filtered to the selected countries, at most `max_rows` (≤ 500) | CC BY 4.0; "World Resources Institute, Global Power Plant Database v1.3.0" | permitted with attribution | one request per selection (≈ 10 MB, budget 20 MB) | the database version is the release (`declared_release` 1.3.0, 2021-06-02). `year_of_capacity_data` is the capacity's effective year. Generation estimates are flagged `estimate: true` |
| GEM | one HTTPS GET of the operator-declared CSV export of one tracker release (`globalenergymonitor.org`) | GEM terms: attribution "Global Energy Monitor, <tracker>, <release>" plus the recorded non-commercial constraint (_verify_) | treated as **non-commercial only** until verified. Notes are cited by wiki URL, not copied | one file per selection. Rows are filtered to the selected countries, at most `max_rows` (≤ 500) | the release label and date are declared. A status differing from the previous release becomes a dated status revision (effective date: the publisher's start/retired year where stated, else the release date) |
| OSM / Overpass | HTTPS GET `https://overpass-api.de/api/interpreter?data=<query>`. The query text is the receipt | ODbL 1.0; "© OpenStreetMap contributors" | share-alike for a derivative database | the query carries `[timeout:60][maxsize:16777216]`, one bbox of at most 1° × 1°, the declared tag set only, and `out meta geom <limit>` with limit ≤ 500. The public instance asks for at most ~10 000 queries/day and 1 GB/day (_verify_). One request per selection | `osm3s.timestamp_osm_base` dates the extract (`extract_timestamp`). Each element keeps `version`, `changeset` and `timestamp`. A later extract with a new element version is a new revision, never an overwrite |
| EIA layers | HTTPS GET `<layer>/query?where=1=1&geometry=<bbox>&geometryType=esriGeometryEnvelope&inSR=4326&outFields=*&f=geojson&resultRecordCount=<n>` | public domain; "Source: U.S. Energy Information Administration" | permitted | `resultRecordCount` ≤ 500, one request per layer and bbox, truncation recorded | the layer publishes no per-feature release date. A declared layer release (label and date) dates the revision, else the retrieval time (labelled `retrieval_time`) |
| ENTSOG | HTTPS GET `/api/v1/connectionpoints?pointKey=…&limit=…` and `/api/v1/operationaldatas?pointKey=…&indicator=Firm Technical&periodType=day&from=…&to=…` | ENTSOG transparency terms (_verify_); "Source: ENTSOG Transparency Platform" | permitted with attribution (_verify_) | at most 20 point keys and a 7-day window per selection | `lastUpdateDateTime` dates each capacity (`provider_last_update`). Capacities keep unit (e.g. `kWh/d`) and direction (`entry`/`exit`) as published |

**Unavailable-access fallback.** A failed step (HTTP error, host outside the
declared set, schema drift) is recorded as a receipt with its failure code.
Stored revisions stay as they are, and the provider reads as stale. Nothing is
inferred.

## Bounded coverage

| Area id | Bounding box (W, S, E, N) | Sources | Asset classes |
| --- | --- | --- | --- |
| `de-lusatia` | 14.2, 51.3, 14.8, 51.8 | OSM Overpass (full tag set), GPPD DEU, GEM coal plants and gas pipelines (Germany) | power plants, generating units, transmission lines, substations, pipelines |
| `de` | 5.86, 47.27, 15.04, 55.06 | GPPD DEU, GEM Coal Plant Tracker and gas pipelines (Germany), ENTSOG points (no geometry) | power plants, pipelines, gas interconnection points |
| `us-gulf-coast` | -94.5, 29.0, -88.8, 31.0 | EIA LNG terminals and natural-gas pipelines, GEM LNG terminals (United States) | LNG terminals, pipelines |
| `us-southern-california` | -118.5, 33.5, -116.0, 35.5 | EIA power plants | power plants |

Rationale:

- Lusatia has lignite plants, transmission lines, substations and gas pipelines inside one small box. All four geometry-bearing sources overlap there, which exercises cross-source reconciliation.
- The Gulf Coast holds the densest set of published LNG terminals.
- Southern California pairs with the Energy Systems pack's EIA-860M plant coverage (CISO). This lets plant-level energy series link by the published EIA plant code.

Mines are covered by the record model (`asset_class: mine`) but no mine
tracker is selected in 1.0.0. That is left to a later bounded selection.

A place query outside every area is answered `not_covered`. Inside an area,
an empty result is "none on record", never "none exist".

## Exclusions

- vulnerability, criticality or dependency assessment;
- asset valuation;
- inferred routes, depths or geocoding of unlocated assets;
- planet-wide or continuous OSM mirroring;
- restricted or CEII-marked layers;
- GEM wiki text beyond a citation;
- editor identities from OSM.
