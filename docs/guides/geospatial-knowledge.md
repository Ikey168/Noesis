# Geospatial knowledge engine

Noesis extends its existing offline gazetteer into a versioned place registry.
Places have stable namespace-scoped identities, immutable revisions, aliases,
historical and multilingual names, source-native identifiers, administrative
parents, provenance, generation, valid time, observation time, producer, and
policy. The original compact gazetteer is loaded as the canonical offline
bootstrap rather than maintained as a second lookup path.

Geometry records are immutable GeoJSON-like points, lines, or polygons with an
explicit `EPSG:4326` coordinate reference system, precision, source, evidence,
administrative hierarchy, dispute status, and validity interval. Multiple
boundary accounts may coexist. Simplification creates a linked derivative and
records the effective tolerance; invalid coordinates, open polygon rings,
unsupported reference systems, and precision errors are rejected.

The deterministic standard-library spatial engine calculates containment,
proximity, intersection, and route length. Longitude handling covers dateline
crossings, while great-circle distance remains usable near the poles. Every
calculation declares its tolerance and CRS and produces a durable,
content-addressed receipt that can be replayed.

Text resolution retains all bounded candidates, name evidence, confidence,
coordinate-hint distance, and the pinned offline method. Unicode folding
supports common transliterations, and `as_of_ms` selects names active at the
requested historical time. Ambiguous and unresolved mentions are never
silently promoted. Saved resolutions accept append-only accept, reject, or
defer reviews, allowing a later reviewer to reverse an earlier selection
without erasing it.

Spatial MCP queries support dateline-aware bounding boxes, radii, containment,
valid-time geometry selection, disputed-boundary filters, filter-bound cursors,
route calculation, and current event-map locations. Results and work are
bounded. Access is separated into `knowledge:geospatial:read`,
`knowledge:geospatial:write`, `knowledge:geospatial:review`, and
`knowledge:geospatial:calculate`.
