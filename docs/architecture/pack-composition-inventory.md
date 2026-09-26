# Pack composition inventory (C01)

Status: inventory for slice C01 of the
[pack and workflow composition architecture](pack-workflow-composition.md),
tracked in [#1789](https://github.com/Ikey168/Noesis/issues/1789).
It records what exists before the composition contracts (C02) were written.
Where a fact could not be settled from the code it is listed under
[Unknowns](#unknowns) instead of guessed.

## Two pack systems exist today

| System | Where | Unit | Enablement | Notes |
| --- | --- | --- | --- | --- |
| Python domain packs | `src/domains/<name>/` registering a `DomainPack` | news, research, economic, legal, political, technical | Process-local `_ENABLED` set loaded from `config/domain_packs.json` or `NOESIS_ENABLED_PACKS` | Carries enrichers, route modules, UI flags, and free-text capability strings. `market` ships helpers but registers no `DomainPack`. |
| Distributable manifests | `packs/<name>/pack.json` (`noesis-pack-v1`) | economics, energy, geospatial, legal, market, osint, political, products, science, technology | `install_manifest` registers and enables a synthesized `DomainPack` in the same process-local registry | Also carry `source_pack`, `query_examples`, and `exclusions`, which v1 validation ignores. |

Both systems share one enabled-state ledger (`src/domains/registry.py`).
Neither resolves a capability string to a provider, dependency, or contract.

## Capability providers

Capability strings are declared by manifests; the providing code is inferred
from the manifest's `query_examples` and the MCP server that registers the tool.

| Bundle | Declared capabilities (examples) | Providing code | Exposure (MCP server / tools) |
| --- | --- | --- | --- |
| Geospatial | `points-inside-boundary-query`, `durable-spatial-projection`, `reviewable-boundary-name-resolution` | `src/kb/geospatial_features.py` (`GeospatialFeatureStore.within`, `replay_within`, `resolve_boundary`), `src/kb/geospatial.py` (places, geometries, spatial relations) | `noesis-knowledge-engine`: `query_geospatial_features_within`, `replay_geospatial_feature_query`, `resolve_geospatial_boundary`, `search_geospatial_knowledge`, `calculate_spatial_relation` |
| OSINT | `origin-aware-corroboration`, `probable-origin-graph`, `source-reliability-cards`, `cited-timeline-reconstruction` | `src/osint/` (`corroborate`, `independence`, `source_reliability`, `timeline_reconstruct`, `relationship_path`, `investigation_audit`), `src/analytics/image_reuse.py` | `noesis-osint` (13 tools) |
| Science / Research | `bounded-scholarly-source-acquisition`, `paper-family-versions`, `cultural-object-place-projection`, `math-literature-records` | `src/domains/research/`, `src/kb/cultural.py`, `src/kb/research_*` modules | `noesis-research` (3 tools) and `noesis-knowledge-engine` |
| Sources | `bounded-*-acquisition` capabilities of every bundle | `src/ingestion/source_packs.py`, `source_pack_runtime.py`, `source_pack_upgrades.py` | `noesis-knowledge-engine`: `install_source_pack`, `run_source_pack_execution`, `preview_source_pack_upgrade_impact`, `apply_source_pack_upgrade`, and related tools |
| Intake | the ten intake modes | `src/kb/intake_modes.py` and `src/kb/intake_*.py` | `noesis-knowledge-engine` intake tools such as `start_intake_mode` |

The catalog (`src/mcp_host/catalog.py`) maps packs to servers with a
hard-coded `_server_pack` that knows only `research_mcp -> research`, and it
derives required data per tool from hard-coded branches in `_required_data`.

## Authoritative stores

Every record type has one owner. The composition layer references these
records and never copies them.

| Record type | Owner module | Tables | Revision addressing |
| --- | --- | --- | --- |
| Places, geometries, spatial results | `src/kb/geospatial.py` | `geospatial_places`, `geospatial_place_revisions`, `geospatial_geometries`, `spatial_receipts` | Geometry and place revisions are immutable IDs. |
| Acquired vector features | `src/kb/geospatial_features.py` | `geospatial_features`, `geospatial_feature_revisions`, `geospatial_feature_current` | `revision_id` per feature, linked to the acquiring `document_id` and page provenance. |
| Documents and their revisions | `src/ingestion` document store | `documents`, `document_revision_records` | `document_id` + `revision_id`; committed by source-pack watermark. |
| Source-pack versions, enablement, health | `src/ingestion/source_packs.py` | `source_pack_versions`, `source_pack_current`, `source_pack_health`, `source_pack_audit` | Immutable `(pack_id, version, manifest_hash)`. |
| Source runs, cursors, budgets, schedules, receipts | `src/ingestion/source_pack_runtime.py` | `source_pack_runs`, `source_pack_checkpoints`, `source_pack_watermarks`, `source_pack_schedules` (one row per pack), `source_pack_license_acceptance` | `run_id` from `(pack_id, run_key)`; receipts are hashed. |
| Source upgrade receipts | `src/ingestion/source_pack_upgrades.py` | `source_pack_upgrade_receipts` | `apply_id` from `(pack_id, principal, apply_key)`. |
| Intake sessions | `src/kb/intake_modes.py` | `intake_sessions`, `intake_session_revisions`, `intake_session_commands` | Optimistic `revision`; references carry an authoritative version. |
| Investigation templates | `src/kb/investigation_templates.py` | `investigation_templates`, `investigation_template_revisions` | Template revision; pins source packs by version. |
| Research projects | `src/kb/research_projects.py` | `research_projects`, `research_project_revisions` | Project revision; `template_origin.source_packs` pins. |
| Research recipes and runs | `src/kb/research_recipes.py` | `research_recipe_revisions`, `research_recipe_runs`, `research_recipe_checkpoints` | `recipe_revision_id` is a content hash; runs checkpoint per step. |
| Schema modules and crosswalks | `src/kb/schema_registry.py` | `knowledge_schema_modules`, `knowledge_schema_dependencies` | `kind:name@version:hash`. |
| Pack manifests (distributable) | `src/domains/pack_registry.py` | Filesystem `<root>/<name>/<version>/pack.json` | Immutable per version unless forced. |
| OSINT observations and origins | `src/osint/` over the warehouse | Warehouse document and claim tables | Uses document identities; no separate revision store. |

## Version and dependency rules to reuse

| Rule | Location | Reuse in composition |
| --- | --- | --- |
| Semantic version parsing and ranges (`^`, `~`, comparator lists, exact) | `_semver`, `_satisfies` in `src/kb/schema_registry.py` | The resolver uses these directly. `*` and `latest` are rejected in retained plans because `_satisfies` treats them as matching anything. |
| Schema module dependencies and consumers | `knowledge_schema_dependencies` | Contract identities are schema-module names and versions. |
| Source-pack version ordering and no-downgrade | `_version`, `preview_upgrade` in `src/ingestion/source_packs.py` | Source pins in plans are exact `(pack_id, version, manifest_hash)`. |
| Source upgrade impact | `SourcePackUpgradeStore._impacts` | Extended with composition dependents in C06. |
| Pack registry version ordering | `_version_key` in `src/domains/pack_registry.py` | Candidate sets list exact published versions. |
| Recipe step contracts | `validate_recipe` in `src/kb/research_recipes.py` | Workflow templates compile to recipes; no second runner. |

## Decisions required by the architecture

**Activation journal storage.** Composition metadata lives in the same DuckDB
warehouse connection that owns source packs, in new `composition_*` tables
(`src/composition/store.py`). This keeps activation receipts next to the
source-pack receipts they reference and uses the existing connection
lifecycle. No other store gains tables. Rationale: the source-pack registry
already uses this connection with explicit `BEGIN`/`COMMIT`, and its receipts
are what reconciliation must read.

**Startup reconciliation boundary.** Reconciliation runs once per process, in
`src/api/app.py` `_load_domain_packs`, after `load_config()`. It reads the
active generation pointer, re-applies that generation's in-process
registrations, and marks any journal entry left in `staged` as `abandoned`
with the previous generation still active. Source-owner operations referenced
by a staged entry are reconciled by reading their owner receipts; nothing is
re-executed.

**Authority during migration.** Until a bundle is cut over, the legacy
registry remains the single authority for its enabled state. After cutover,
`src/domains/registry.py` delegates enable and disable for that bundle to the
coordinator, or raises a compatibility error. The environment flag
`NOESIS_COMPOSITION_LIFECYCLE=legacy` rolls bindings back to the legacy path.

## Unknowns

- OSINT tools read warehouse tables directly and have no revision-addressed
  store of their own. Plans reference OSINT outputs by document identity only.
  Revision addressing is reported as unsupported for those record kinds.
- `market` has helper modules under `src/domains/market` and a distributable
  manifest, but no registered Python `DomainPack`. Its migration uses the
  manifest alone.
- The `energy` manifest declares no capabilities. It adapts with an empty
  capability set and contributes only its provisioning template.
- Live provider availability for any bundle is out of scope for this
  inventory and for the offline composition proof.
