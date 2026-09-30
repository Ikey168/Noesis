# Geospatial critical infrastructure guide

The Geospatial pack's optional `infrastructure` feature is off by default. Given a place or an operator and a
date, it answers which published infrastructure assets exist. The asset classes are:

- power plants and generating units;
- pipelines and LNG terminals;
- transmission lines and substations;
- gas interconnection points and mines.

Each answer includes status history (proposed, construction, operating, mothballed, retired, cancelled),
capacity, and owner and operator assertions as published. Every value cites its source, release and as-of time.
Tracking issue #2223.

- Source decisions and bounded coverage: [source audit](../development/infrastructure-evidence/source-audit.md).
- Offline and live evidence: [evidence](../development/infrastructure-evidence/README.md).

## Sources

| Source | Acquired as | Keyed by | Notes |
| --- | --- | --- | --- |
| WRI Global Power Plant Database | v1.3.0 release CSV, selected countries | `gppd_idnr` | WEPP ids kept; generation estimates flagged as estimates |
| Global Energy Monitor | one declared tracker release file (coal units, gas pipelines, LNG terminals) | GEM unit, project or terminal id | status changes across releases become dated status revisions; owners and parents with shares as published; wiki notes cited by URL; every record carries the non-commercial constraint until the terms are verified |
| OpenStreetMap (Overpass) | one bbox (≤ 1°×1°) and tag set per selection | element type/id with version and changeset | the query text and `timestamp_osm_base` are the receipt; editor identities are dropped; ODbL share-alike |
| EIA energy infrastructure layers | ArcGIS layer query in a bbox (GeoJSON) | EIA plant code or layer feature id | LNG terminal dockets are kept as cited references |
| ENTSOG | points and firm technical capacity for ≤ 20 point keys | `pointKey` | capacities keep unit and entry/exit direction; no coordinates are published, so none are invented |

Acquisition runs through the source-pack runtime. The source pack is
`config/source_packs/geospatial-infrastructure.json` and uses the `infrastructure` connector. Offline, the MCP
tool `acquire_infrastructure_source` runs one bounded selection with one receipt per step. Geometries go into the
existing geospatial store with a CRS and precision receipt.

## Answers

- `infrastructure_assets_in_place` answers for a polygon place, a geometry or a bbox, as of a date.
  - `known_by` limits the answer to what was published by then.
  - Status is resolved by the publisher's effective date where stated, else by first publication.
  - Capacities are resolved per metric and direction.
  - Owners come from the revision published by the date.
- `infrastructure_assets_of_operator` answers for a Corporate Ownership record key or entity id, reached
  through **accepted** operator matches only. It also accepts a published name, which is labelled as a
  published string rather than an identity.
- Assets are grouped only by accepted `same_asset` matches. Disagreeing status or capacity is listed side by
  side. GEM units that cite a GPPD plant appear as its components and are never summed.
- An area outside the coverage is `not_covered`. An empty covered area is "none on record". Assets without
  coordinates are listed as `not_located`.
- Pass `evidence_bundle=true` to export a `noesis-evidence-bundle-v1`.

## Identity and links

- `propose_infrastructure_asset_matches` does two things:
  - Published cross-references (GEM `WRI:`/`WEPP:` ids, OSM `ref:gppd`) become identifier matches.
  - Proximity, name and class together produce review candidates, with a receipted distance and a score.
- `review_infrastructure_asset_match` accepts, rejects or reverts a match. Reviews are append-only.
- `propose_infrastructure_operator_matches` offers published operators and owners to Corporate Ownership through
  the shared identity state machine: identifier, name plus jurisdiction, or the never-acceptable similar-name.
  Assertions are never rewritten.
- `link_infrastructure_records` links assets by explicit identifier or citation only:
  - to Energy Systems series, by EIA plant code;
  - to legal works, by the dockets or permits a publisher cites;
  - to environment facilities, by shared identifiers.

  Name overlap only creates a candidate. Packs that are not installed are skipped.

## Monitoring

`create_infrastructure_monitor` watches a place, an operator or an asset. `run_infrastructure_monitor`
evaluates it at a committed watermark and emits the following notifications, each citing both revisions:

- `new_asset`
- `status_change`
- `capacity_change`
- `ownership_change`
- `no_longer_in_view`
- `stale_source`

## Composition

`packs/geospatial/providers/geospatial.infrastructure.json` declares the `geospatial.infrastructure` provider.
The optional features, all off by default, are:

- `infrastructure`
- `infrastructure-ownership` (Corporate Ownership)
- `infrastructure-citation-links` (Legal)

Energy Systems and Climate and Environment depend on Geospatial, so they cannot be declared requirements without a cycle.
Their links are made read-only when those bundles are installed and are skipped otherwise; `infrastructure_readiness`
reports which optional packs are present.

A missing Corporate Ownership or Legal provider is a visible omission of its feature only.

## Never

- vulnerability, criticality or dependency assessment;
- asset valuation;
- inferred routes or geocoding;
- planet-wide or continuous OSM mirroring;
- restricted (CEII) layers.
