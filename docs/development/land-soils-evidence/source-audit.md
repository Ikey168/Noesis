# Land, soils and geology: source-contract audit and bounded coverage (LN01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

This audit sets out, per source, what the Climate and Environment bundle's
proposed `environment.land` provider (subdomain `land-soils-geology`,
ADR-005) may acquire, how, and on what terms. No per-track tracker or delivery
issue exists yet; they are opened once this audit names a surviving source,
which it does (see the access decisions). The live-validation issue is opened
with them.

**It was written without network access: the publisher hosts
(`land.copernicus.eu`, `land.discomap.eea.europa.eu`, `sgx.geodatenzentrum.de`,
`fra-data.fao.org`, `esdac.jrc.ec.europa.eu`, `services.bgr.de`,
`www.bgs.ac.uk`) are blocked by this runtime's egress proxy (verified
2026-10-03), so terms, endpoints, layer names and field names were not
re-verified live.** They come from the gap table's candidates and the
publishers' documentation as the author knows it. Every item marked _verify_
must be checked against the live pages, the live terms and a real response
before the first dated live run. No source is `live` until that run exists.

**No machine-readable copy exists yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and `LIVE_VERIFICATION` will be added
in `src/ingestion/land_sources.py`, and `MINIMISATION` in
`src/kb/land_records.py`, by the track's acquisition issues. They must match
this audit; a difference is a defect in the code or a reason to amend this
audit first.

## Home and source pack

The land sources are planned as their own source pack
(`climate-environment-land`, proposed) inside
`packs/climate-environment/source_packs/`, pinned only by `environment.land`,
optional and off by default, so existing pins, fixtures and hashes stay
byte-identical. Taxonomy shapes: `places-geometry` (land-cover polygons and
geological or soil map units as published features) and `statistical-series`
(forest statistics per assessment round).

## Scope boundary (all sources)

- No image classification, no land-cover classes of our own and no recoding of
  published codes beyond the publisher's own nomenclature levels.
- No land-cover change derived by differencing two editions: change is only
  what a published change layer states.
- No interpolation of soil properties, no geological interpretation, and no
  deforestation, degradation or soil-health judgement beyond the source.
- No area statistics computed by Noesis; published areas are quoted.

## Access decisions

| Source | Publisher | Delivers | Decision |
| --- | --- | --- | --- |
| CORINE Land Cover, EEA vector service | EEA for the Copernicus Land Monitoring Service (CLMS) | CLC status polygons (25 ha) per edition and the published change layers | selected, `unverified-live` (precondition below) |
| CORINE Land Cover, CLMS download API | CLMS | whole-dataset or area-clipped downloads | `gated-not-granted` |
| CLC5 (BKG) | Federal Agency for Cartography and Geodesy | German CORINE version at 5 ha, WFS | selected, `unverified-live` |
| FAO Forest Resources Assessment | FAO | forest area and characteristics per country and assessment round | selected, `unverified-live` |
| ESDAC soil data | European Commission, JRC | European Soil Database, LUCAS topsoil and derived maps | `not-implemented` (terms) |
| BGR overview maps | Federal Institute for Geosciences and Natural Resources | geological (GUEK250) and soil (BUEK200) map units as features | selected, `unverified-live` (precondition below) |
| BGS | British Geological Survey | 1:625 000 geology; borehole index | `not-implemented` in first coverage |

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| CLC, EEA vector service | ArcGIS REST feature query, `https://land.discomap.eea.europa.eu/arcgis/rest/services/Land/CLC2018_WM/MapServer/0/query?geometry=<bbox>&geometryType=esriGeometryEnvelope&inSR=4326&outFields=Code_18,ID,Area_Ha&f=geojson`, one service per edition and change layer _verify_ service names, layer ids, fields and whether `query` is enabled | none | Copernicus data policy (Regulation (EU) No 377/2014, Delegated Regulation (EU) No 1159/2013): free, full and open with attribution "(c) European Union, Copernicus Land Monitoring Service, European Environment Agency" _verify_ wording | no published quota _verify_; server `maxRecordCount` pages each query _verify_ | each edition (reference years 1990 to 2018; a 2024 edition announced _verify_) is a separate vintage; a product version (for example `v2020_20u1`) re-released for an edition is a new revision of that edition _verify_; change layers (`CHA`) are separate datasets, never derived. **Precondition:** if the live check finds no queryable vector service, EU CLC moves to `not-implemented` for first coverage and CLC5 alone carries land cover |
| CLC, CLMS download API | `https://land.copernicus.eu` dataset downloads, asynchronous _verify_ | EU Login account and a CLMS API service key, as the secret reference `NOESIS_CLMS_SERVICE_KEY`; never in manifests, receipts or records | as above | per-account queue _verify_ | as above. **Gated, not granted:** no account exists for this deployment, and downloads are Europe-wide or large area clips that the caps forbid mirroring; off by default |
| CLC5 (BKG) | WFS 2.0.0 GetFeature, `https://sgx.geodatenzentrum.de/wfs_clc5_2018` with a BBOX filter and `COUNT`/`STARTINDEX` paging _verify_ service and type names | none | Datenlizenz Deutschland - Namensnennung 2.0 with the BKG source note "(c) GeoBasis-DE / BKG" and the reference year _verify_ | no published quota _verify_ | one service per reference year (2012, 2018) _verify_; each is a vintage. CLC5 is BKG's own product at a finer mapping unit and is **never merged** with EU CLC; answers show both with their publisher |
| FAO FRA | FRA platform bulk download (CSV in a ZIP per assessment round) or its data API, `https://fra-data.fao.org/...` _verify_ paths and column names | none | FAO terms for the FRA platform: CC BY 4.0 or CC BY-NC-SA 3.0 IGO _verify_; cite "FAO, Global Forest Resources Assessment" with the round | no published quota _verify_; one file per round | each round (FRA 2020, FRA 2025 _verify_) re-reports the whole series (1990, 2000, 2010, 2015, 2020, 2025), so the same year differs between rounds: a round is a separate vintage and rounds are never spliced; a corrected file within a round is a new revision dated by the platform's release note, else the retrieval time. Only declared countries and variables are kept from the file; the file itself is hashed, not stored |
| ESDAC | data request form after registration _verify_ | registration | JRC licence for ESDAC datasets: use by the requester; no distribution to third parties; often non-commercial _verify_ per dataset | n/a | **Not implemented:** stored values could not be cited or exported in evidence bundles. Kept as a documented contract so queries report soils data from ESDAC as unavailable, not empty |
| BGR GUEK250 and BUEK200 | WFS GetFeature on `https://services.bgr.de/wfs/...` with a BBOX filter _verify_ whether a WFS exists for each layer (WMS services are known) | none | BGR terms of use; Datenlizenz Deutschland - Namensnennung 2.0 or CC BY 4.0 _verify_; cite BGR and the cooperating state surveys as stated | no published quota _verify_ | each map is an edition with a version date _verify_; a new edition is a new vintage of the unit features. **Precondition:** WMS images and GetFeatureInfo answers are not records; a layer without a WFS moves to `not-implemented` |
| BGS | OGC services and downloads for BGS Geology 625k _verify_ | none | Open Government Licence for 625k geology _verify_; borehole scans under separate terms _verify_ | _verify_ | **Not implemented in first coverage:** outside the bounded area; a candidate for a later version (geology only, borehole index excluded) |

**Unavailable-access fallback.** A failed request (HTTP error, redirect to
another host, service exception, schema drift, a response over the byte cap)
fails that source's run with its code and a receipt; earlier vintages stay
current, nothing is marked removed, and readiness reports the source as
`stale`.

## Record shape and reuse

- **Places and geometry:** land-cover polygons and map units are published
  features keyed by publisher, layer, edition and native id, through
  `WfsFeatureAdapter` in `src/ingestion/wfs_api.py` (paging, `numberMatched`,
  service exceptions), `FeatureBudget` and `decode_feature_collection` in
  `src/ingestion/geojson_features.py`, and `GeospatialFeatureStore` in
  `src/kb/geospatial_features.py` (precision policy and the within algorithm).
  The ArcGIS query needs a small adapter onto the same feature records.
- **Statistical series:** FRA values per country, variable, year and round on
  the scopes and digests of `src/kb/environment_records.py`; round comparison
  reuses `src/kb/environment_vintages.py`.
- **Places:** containment answers use the Berlin district boundaries of the
  `geospatial-berlin` source pack
  (`packs/geospatial/source_packs/geospatial-berlin-1.3.0.json`) and
  `src/kb/geospatial.py`.
- **Links (by citation only):** agrifood (`src/kb/agrifood_links.py`; FAOSTAT
  land-use series in `src/ingestion/agrifood_sources.py` stay agrifood's and
  are not duplicated), hazards (`src/kb/hazards_links.py`) and biodiversity
  (`src/kb/biodiversity_links.py`). Absent providers degrade to an unavailable
  report; no overlay-derived or causal links.

## Data minimisation

Land cover, forest statistics and map units are not about people. Decision:

- **Stored:** published codes, labels, areas, editions and organisational
  names (publisher, cooperating survey).
- **Excluded:** borehole records altogether in first coverage, and in any later
  version the owner, client, landowner, driller and site-contact fields of a
  borehole index (BGS, state surveys under the Geologiedatengesetz); names of
  FRA national correspondents and report authors; any cadastral parcel or owner
  data. Dropped keys are listed in the page receipt and rejected at write time.
- **Retention:** vintages are kept for provenance; removals by a source are
  tombstone revisions.
- **Who may query:** principals with `knowledge:environment:read` and namespace
  access.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| CLC (EEA) | one declared box on the south-east Berlin and Brandenburg boundary, about 13.55-13.80 E, 52.38-52.50 N; status 2012 and 2018, change 2012-2018 | 3 layers, 2 000 features and 20 MB per layer |
| CLC5 (BKG) | the same box; reference years 2012 and 2018 | 2 layers, 5 000 features and 20 MB per layer |
| FAO FRA | Germany (DEU) and Poland (POL); forest area, other wooded land, naturally regenerating and planted forest; rounds 2020 and 2025 | 2 countries, 4 variables, 2 rounds, 25 MB per file |
| BGR | the same box; GUEK250 and BUEK200 units | 2 layers, 2 000 features each |
| All | nothing enumerated; every refresh stays inside the declared selection | 20 pages per source per run |

Justification: one small box crossing the Berlin boundary shows urban,
forest, water and agricultural classes, both CORINE products side by side
without merging, and containment in the existing Berlin district boundaries.
Germany and its eastern neighbour show two FRA rounds restating the same years.
Each further box, layer or country is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| CLC (EEA vector service) | `unverified-live` | - | none yet; queryable service unconfirmed |
| CLC (CLMS download API) | `gated-not-granted` | - | no EU Login account or service key configured |
| CLC5 (BKG) | `unverified-live` | - | none yet |
| FAO FRA | `unverified-live` | - | none yet |
| ESDAC | `not-implemented` | - | request-based, no-redistribution terms _verify_ |
| BGR GUEK250 and BUEK200 | `unverified-live` | - | none yet; WFS availability unconfirmed |
| BGS | `not-implemented` | - | outside first coverage |

The fixtures will be authored, not captured: synthetic codes, polygons and
values for reference years 2094-2097 and releases dated 2098-2099, so nothing
can be mistaken for a published figure.
