# Geospatial vector source pack

The geospatial pack acquires bounded public vector datasets, keeps each
feature's source identity and revision history, projects the features into the
existing geospatial store, and answers "which points lie inside this
boundary?" with inspectable evidence and a replayable receipt.

The first source profile is Berlin: district boundaries and school locations
from the Geoportal Berlin WFS services.

| Piece | Location |
| --- | --- |
| Domain pack (vocabulary, capabilities, query examples, exclusions) | `packs/geospatial/pack.json` |
| Source pack (pinned WFS contracts, terms, budgets, fixtures) | `config/source_packs/geospatial.json` |
| WFS 2.0.0 adapter | `src/ingestion/wfs_api.py` |
| GeoJSON decoding, HTTPS GeoJSON adapter, local import | `src/ingestion/geojson_features.py` |
| Feature revisions, projection, snapshots, queries | `src/kb/geospatial_features.py` |
| Offline transform fallback | `src/integrations/spatial.py` |
| MCP tools | `tools/knowledge_engine_mcp/geospatial_features.py` |
| Contracts | `noesis-geospatial-feature-v1`, `-feature-snapshot-v1`, `-feature-query-v1`, `-pack-readiness-v1` |

## Pinned Berlin layers

Both services were inspected on 2026-09-25 (`GetCapabilities`,
`DescribeFeatureType`). They run GeoServer WFS 2.0.0 with result paging and
native CRS EPSG:25833 (ETRS89 / UTM 33N, easting/northing).

| Source | Type name | Stable native ID | Count | Title property |
| --- | --- | --- | --- | --- |
| `berlin-bezirksgrenzen` | `alkis_bezirke:bezirksgrenzen` | `bezirksgrenzen.<AGS>` | 12 MultiPolygons | `namgem` |
| `berlin-schulen` | `schulen:schulen` | `schulen.<BSN>` | 930 Points | `schulname` |

- **Requests.** `VERSION=2.0.0`, `OUTPUTFORMAT=application/json`,
  `SRSNAME=urn:ogc:def:crs:EPSG::25833`, `SORTBY=name` (districts) or `bsn`
  (schools), then `COUNT`/`STARTINDEX` paging. GML is not needed because the
  service offers GeoJSON.
- **Axis order** is declared as `east_north`. The capture also showed this
  server returning EPSG:4258 in longitude/latitude order, even though the URN
  defines latitude/longitude. Axis order is therefore always declared, never
  inferred from the CRS name.
- **Terms.** Both layers use Datenlizenz Deutschland – Zero – 2.0
  (`dl-de-zero-2.0`), which requires no attribution. Terms are still accepted
  per source through the runtime licence mechanism, and the pack records a
  courtesy attribution.
- **Metadata.** ISO metadata records are linked per source
  (`geospatial.metadata_url`).
- **Precision.** The WFS capabilities and the GeoJSON declare no positional
  accuracy, so precision is recorded as `unknown`.
- **Provider quirks** reproduced in fixtures:
  - A `next` link is returned past the last page, so paging stops on
    `numberMatched`.
  - `SORTBY=gml:id` is rejected with `InvalidParameterValue`.
- **Alternative layers.** If a layer becomes unusable, the same service family
  also offers `alkis_ortsteile:ortsteile` (localities) and
  `schulen:schulen_esb` (school catchments). Either can be swapped in by
  pinning a new source entry.

## Install and operate

1. Install and enable the source pack with the existing mechanisms
   (`install_source_pack`, `set_source_pack_enabled`). Then record terms
   acceptance for each source (`accept_source_pack_license`). Without it,
   live runs are blocked.
2. Check `inspect_geospatial_pack_readiness`, which reports:
   - whether the pack is installed and enabled
   - terms acceptance per source
   - the transform backend
   - missing optional dependencies. Without `pyproj`, only EPSG:4326 and the
     ETRS89 / WGS 84 UTM zones can be projected. Without `shapely`, exact
     multipart intersects/covers and simplification are unavailable.
3. Run the pack (`run_source_pack_execution`, operation `features`). Pass
   `network=live` for real acquisition. Offline runs replay the pinned
   native captures through the same adapter.
4. Query:
   - `query_geospatial_features_within` for points inside a boundary
   - `search_geospatial_knowledge` for bounding boxes
   - `calculate_spatial_relation` for proximity to a point
5. Inspect the evidence behind a result with `inspect_geospatial_feature` and
   `inspect_geospatial_feature_coverage`, and verify a query with
   `replay_geospatial_feature_query`.

Disabling the pack stops its acquisition. Projected features and
geometries stay readable, and other packs' spatial use (the gazetteer,
event maps) is unaffected.

### Local GeoJSON import

`import_geospatial_features` imports one bounded FeatureCollection into the
caller's namespace. The caller must hold `knowledge:geospatial:write`, and
imports into `global` are refused. The collection can be passed inline, or
as a path, which must resolve inside `NOESIS_GEOSPATIAL_IMPORT_ROOT`. Imports
into one namespace are invisible to other namespaces.

## Semantics you can rely on

- **Identity.** A feature is identified by provider + collection + native
  ID. Features are never merged across providers, and they are not named
  places.
- **Rejections.** A feature with no ID, a duplicate ID, no geometry, an
  unsupported geometry type (only Point, LineString, Polygon and MultiPolygon
  are supported) or invalid geometry is quarantined with its reason. It is
  never dropped silently and never given an invented identity.
- **Revisions.** Unchanged re-acquisition creates no new revision or geometry.
  A change creates a new revision. Each revision keeps:
  - the source properties and source-CRS geometry
  - the transform receipt
  - the acquisition document ID
  - the page response hash and run ID
- **Time.** Acquisition time and the provider timestamp are recorded, but
  neither is used as valid time. Valid time stays `unknown` unless the source
  declares it.
- **Removal.** A feature is removed only by a complete snapshot of an
  unbounded scope, or by an explicit provider tombstone. The following never
  remove anything:
  - bbox-limited runs
  - interrupted or failed paging
  - a collection that changes size while paging
  - projection failures
  - snapshots older than the current one

  A removal records when the absence was observed. It does not invent an end
  of validity.
- **Recovery.** Projection runs before each page's checkpoint and is
  idempotent per source revision. After a crash, resuming the run replays the
  page without losing or duplicating features. Features that failed
  projection (for example, a missing transform) keep their native record and
  can be retried with `retry_geospatial_feature_projection`.
- **Queries.** Points inside a boundary are decided by exact ring parity on
  the stored WGS84 coordinates, and points on the boundary edge count as
  inside. A boundary name that matches more than one active polygon returns
  `needs_review` with the candidates and no result. `proximity` measures
  distance to the nearest stored vertex, and `route` measures the length of
  the input path. Neither is a boundary distance or road routing.
- **Transforms.** With `pyproj` installed, transforms use PROJ with network
  access disabled. Otherwise a stdlib inverse transverse Mercator (Krüger
  series, order 6) handles ETRS89 and WGS 84 UTM zones. The ETRS89 → WGS 84
  step is the EPSG:1149 null transformation, with 1 m accuracy recorded in
  the receipt. Against the provider's own EPSG:4258 output for all 930
  schools, the fallback differs by at most 0.6 mm.

## Evidence

- **Offline** (`tests/unit/domains/test_geospatial_pack.py` and the unit
  suites):
  - both captured layers replayed through the real adapter, runtime,
    projection and queries
  - every school's computed district equals its provider-declared `bezirk`
  - idempotency, changed revisions, incomplete snapshots, crash recovery,
    tombstones, quarantine, holes, MultiPolygons, axis order, projected CRS,
    budgets and namespace isolation are all covered
- **Live** (`config/geospatial/acceptance/live-berlin.json`, produced by
  `scripts/geospatial_live_berlin.py`): a dated bounded run against
  gdi.berlin.de. It records endpoints, per-page response hashes and provider
  timestamps, counts, elapsed time, snapshot completeness, per-district
  membership against the provider's declared district, and receipt replay.

## Exclusions

- OGC API Features
- OSM/Overpass
- global gazetteer imports
- raster, satellite and terrain processing
- tiles and basemaps
- road-network routing
- new spatial databases
- GIS editing
