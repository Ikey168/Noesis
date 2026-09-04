# Knowledge quality

Noesis quality assessments keep coverage, provenance, independence, freshness,
contradiction, methodology, reproducibility, and uncertainty separate. Missing
dimensions remain unknown. Versioned policies may define transparent defaults,
domain overrides, weights, thresholds, and calibration datasets, but a
policy-specific composite is never presented as a universal truth score.

Assessments support sources, documents, claims, entities, events, datasets,
answers, and evidence bundles. Every dimension retains exact input lineage;
inaccessible and retracted inputs become visible flags. Incremental generations
produce new assessments and deterministic replay hashes.

Collection aggregation discounts correlated independence groups and returns an
uncertainty interval. Small calibration samples and distribution drift are
warnings. Retrieval ranking is stable at threshold edges, exposes all
dimensions and user overrides, and always returns low-scored evidence with
`retained: true` instead of silently erasing it. Policy simulation is
side-effect free.

MCP scopes are `knowledge:quality:read`, `knowledge:quality:write`,
`knowledge:quality:calculate`, and `knowledge:quality:review`. Bounded health
inspection reports missing dimensions and degraded inputs across all six
research domains; human-evaluation fixtures are referenced as calibration data,
not treated as hidden ground truth.
