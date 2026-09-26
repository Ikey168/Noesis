# Geospatial pack scope

Status: implemented 2026-09-25. See the [geospatial pack guide](../guides/geospatial-pack.md).
Offline acceptance and a dated bounded live run against both Berlin layers are
recorded separately (`config/geospatial/acceptance/live-berlin.json`).

## Delivery state (2026-09-26)

Audited against the repository on 2026-09-26 (C09.1, [Ikey168/Noesis#1840](https://github.com/Ikey168/Noesis/issues/1840)). Every item below cites a file or tool that exists in this checkout; where this section and older text disagree, this section is current.

- **Shipped:** #1696–#1705 — pinned Berlin WFS layers (`config/source_packs/geospatial.json`), bounded GeoJSON import (`import_geospatial_features`), the native WFS adapter (`src/ingestion/wfs_api.py`), provenance-preserving features and projection (`src/kb/geospatial_features.py`), refresh/removal semantics, the pack and source profile (`packs/geospatial/pack.json`), source-aware queries (`query_geospatial_features_within`, `replay_geospatial_feature_query`) and offline acceptance. The live Berlin validation ran: `config/geospatial/acceptance/live-berlin.json` records a complete import with accepted terms hashes, which supersedes the earlier "no live feature import was performed" note.
- **Not shipped:** the OGC API Features adapter planned after v1.
- **Composition:** `packs/geospatial` is composition-managed (C08, C09.3). It contributes `noesis.geospatial` and `noesis.transit` (`config/composition/providers/`), and its declared labels are aliased to provider capabilities in `packs/geospatial/composition.json`.
- **Composition dependency of remaining work:** none. The OGC API Features adapter is a new source connector behind the existing `sources.acquire` capability and `noesis.geospatial` provider.

## GitHub implementation issues

Tracking: [#1695](https://github.com/Ikey168/Noesis/issues/1695).
Children carry acceptance criteria and explicit prerequisite issue references.

Reviewed on 2026-09-25: the ten existing children cover the agreed v1 scope;
no duplicate implementation issues were added. Related Products work is tracked
in [#1706](https://github.com/Ikey168/Noesis/issues/1706). Share reusable runtime
improvements without coupling the two packs' delivery or acceptance.

- [x] [#1696](https://github.com/Ikey168/Noesis/issues/1696) — Select and pin two Berlin vector source contracts.
- [x] [#1697](https://github.com/Ikey168/Noesis/issues/1697) — Add bounded GeoJSON acquisition and import.
- [x] [#1698](https://github.com/Ikey168/Noesis/issues/1698) — Implement the native WFS adapter.
- [x] [#1699](https://github.com/Ikey168/Noesis/issues/1699) — Define provenance-preserving spatial normalization.
- [x] [#1700](https://github.com/Ikey168/Noesis/issues/1700) — Project acquired revisions into the spatial store.
- [x] [#1701](https://github.com/Ikey168/Noesis/issues/1701) — Implement safe snapshot refresh and removal semantics.
- [x] [#1702](https://github.com/Ikey168/Noesis/issues/1702) — Package capabilities and the Berlin source profile.
- [x] [#1703](https://github.com/Ikey168/Noesis/issues/1703) — Expose source-aware query workflows.
- [x] [#1704](https://github.com/Ikey168/Noesis/issues/1704) — Add end-to-end offline acceptance coverage.
- [x] [#1705](https://github.com/Ikey168/Noesis/issues/1705) — Validate live Berlin layers and publish the demo.

## Outcome

Enable a geospatial pack, acquire a bounded public vector dataset, and query
its places and geometries through existing Noesis tools with source attribution,
version history, and reproducible spatial results.

The pack groups a distinct resource ecosystem. Existing news, political,
economic, and research records can refer to these places without duplicating
their ingestion or moving them into a new silo.

Initial demonstration: import an official Berlin boundary layer and a point
layer, find the points inside a selected boundary, and inspect the evidence and
dataset revision behind each result. Berlin is a proposed first source profile;
the adapters and pack remain geographically reusable.

## Existing foundation to reuse

| Capability | Existing implementation | Scope implication |
| --- | --- | --- |
| Place identities, aliases, historical names, reviewable resolution | `src/kb/geospatial.py` | Extend the existing registry; no parallel place store. |
| Versioned geometry, provenance, validity, disputed boundaries | `src/kb/geospatial.py`; geospatial contracts | Reuse current records and namespaces. |
| Point, LineString, Polygon, MultiPolygon storage | `src/kb/geospatial.py::_validate_geometry` | Explicitly constrain incoming geometry types. |
| Coordinate conversion and topology | `src/integrations/spatial.py` | Reuse optional pyproj/Shapely paths and their receipts. |
| Spatial searches, calculation receipts, event maps | `tools/knowledge_engine_mcp/server.py` | Reuse tools, scopes, and bounded query behavior. |
| Pack installation, vocabulary, panels, enable/disable | `src/domains/pack_format.py`, `pack_install.py` | Add a thin declarative domain manifest. |
| Acquisition budgets, scheduling, retries, quarantine, license receipts | `src/ingestion/source_pack_runtime.py` | Extend this runtime rather than create a new scheduler. |
| Regression coverage | `tests/unit/kb/test_geospatial.py`, `tests/unit/tools/test_geospatial_mcp.py`, `tests/unit/analytics/test_geospatial.py` | Run these alongside new integration tests. |

High platform reuse is credible; a numerical claim of 90% is not measured.
Most new effort is native acquisition, mapping, and integration validation.
Existing calculations also have boundaries: route calculation measures an
input path, not road-network routing; the standard-library proximity operation
uses geometry vertices, not a general exact distance-to-boundary algorithm.
V1 query templates must respect these semantics.

## V1 deliverables

1. **Pack declarations.** Add `packs/geospatial/pack.json` using
   `noesis-pack-v1`, plus `config/source_packs/geospatial.json` using
   `noesis-source-pack-v1`. Declare existing capabilities, contract versions,
   planner vocabulary, source attribution, budgets, and fixture receipts.
   Installing a manifest must not advertise an unimplemented native adapter.
   Disabling the pack stops its acquisition/routing contributions without
   deleting shared place evidence or disabling other packs' spatial use.

2. **Bounded vector acquisition.** Implement GeoJSON FeatureCollection
   acquisition/import and one native WFS adapter for the chosen Berlin layers.
   Support explicit layer selection, bounding box, page/feature/byte limits,
   safe pagination, resumable acquisition, and declared source CRS/axis order.
   Inspect service capabilities before pinning its version and output format;
   if a chosen layer requires GML, implement and fixture that precise mapping
   rather than treating the endpoint as generic JSON REST.

3. **Feature-to-store bridge.** The current source-pack normalizer produces
   documents and retains native records in metadata; that is not a spatial
   import. Add an idempotent projection into `GeospatialStore`, linked to the
   acquired source revision. Define feature identity as provider + collection
   + native ID. Preserve properties, source URL/hash, dataset revision,
   attribution, geometry, precision policy, observation time, and any declared
   valid time. Unknown precision/time must not become invented source facts.
   Keep geometric features distinct from named places; a feature need not
   create a place. Retain cross-source identities until an explicit mapping
   or review justifies linking them.

4. **Refresh semantics.** Repeat imports create no duplicate evidence. Changed
   features produce traceable revisions. Only a complete declared snapshot or
   explicit provider deletion can establish removal; absence from a bounded
   page or bbox does not. Incomplete runs retain retry state and cannot advance
   a completed snapshot marker. Coordinate source acquisition and spatial
   projection so a crash can resume without lost or duplicated features.

5. **Usable query entry points.** Add pack vocabulary and examples for places
   in a box, points inside a boundary, and point proximity. Surface dataset
   scope, freshness, attribution, and resolution ambiguity through existing
   result/panel mechanisms. A dedicated map renderer is not a v1 dependency.

## First source profile

Use two explicitly selected Berlin public vector layers: an administrative
boundary layer and a public-facility point layer. Exact layer IDs, native
formats, licenses, stable feature IDs, precision metadata, and paging behavior
remain implementation discovery gates, not asserted capabilities.

Berlin documents WFS vector downloads and a catalogue/Geoportal for finding
services; it also notes that some datasets use other download mechanisms.
See the [Berlin portal FAQ](https://www.berlin.de/umweltatlas/en/our-portal/faq/)
and [geodata infrastructure guide](https://www.berlin.de/sen/stadt/stadtdaten/geoinformation/geodateninfrastruktur/).

After v1, add an OGC API Features adapter to broaden compatible providers.
Its collection/item model and GeoJSON responses are a natural extension, but
are not interchangeable with WFS. See the
[OGC API Features overview](https://ogcapi.ogc.org/features/overview.html).
Provider documentation was reviewed for this scope; no live feature import or
dataset-specific terms verification was performed.

## Acceptance criteria

- Pack validation, install/disable behavior, and offline fixture replay pass.
- Both sample layers reach spatial storage with preserved source identities,
  native revisions, attribution, and acquisition receipts.
- A repeated import is idempotent; updates retain history; a simulated crash
  between acquisition and projection resumes correctly.
- Incomplete pagination and bbox-limited collection do not imply deletion.
- Fixtures exercise coordinate axis order, projected CRS, polygon holes,
  MultiPolygon, malformed/unsupported geometries, and budget exhaustion.
- Unsupported geometries are quarantined explicitly, never silently flattened.
- Missing optional spatial dependencies produce an actionable capability status.
- A known point-inside-boundary query returns the expected set and source
  references; an ambiguous place name remains unresolved until reviewed.
- Existing spatial and source-pack regression tests pass. A separately recorded
  bounded live run validates the two selected layers before production readiness
  is claimed; offline success alone is insufficient.

## Delivery sequence and boundaries

1. Select the two layers and pin their protocol/metadata contracts and fixtures.
2. Implement acquisition plus the durable spatial projection bridge.
3. Add manifests, query examples, and the end-to-end validation/demo.

This is a moderate integration project, not just a manifest edit. The principal
unknowns are source format fidelity, refresh/delete semantics, and transactional
handoff into the spatial store. Resolve those before estimating calendar effort.

Deferred: OSM/Overpass ingestion, global gazetteer acquisition, raster/satellite
processing, terrain analysis, tiles/basemaps, road-network routing, unrestricted
global collection, new spatial databases, and a full GIS editing interface.
These can be separate increments without expanding the v1 acceptance surface.
