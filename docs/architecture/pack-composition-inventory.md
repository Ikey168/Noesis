# Pack composition inventory (C01)

Status: inventory as of 2026-09-26. This is documentation only; nothing here
changes runtime behavior. Tracking: [#1789](https://github.com/Ikey168/Noesis/issues/1789)
under [#1788](https://github.com/Ikey168/Noesis/issues/1788). Architecture:
[pack and workflow composition](pack-workflow-composition.md). Decision record:
[ADR-003](decisions/ADR-003-composition-activation-journal.md).

Later slices cite this inventory instead of re-deriving ownership. Unknowns are
listed at the end with the decision each one needs; nothing below is guessed.

## Surface summary

| Surface | Provider code | Authoritative stores | Exposure | Version rules |
| --- | --- | --- | --- | --- |
| OSINT | `src/osint/*` (origin, corroboration, contradictions, reliability, timeline, provenance, dossier, investigations); `src/kb/source_identity.py`; `src/kb/events.py`, `src/kb/event_dossiers.py` | `documents` + `document_revision_records` (evidence); `document_origin_*`, `reporting_origins` (origin graph); `source_identities` + revisions; event records/dossiers | `noesis-osint` (13 tools); source-identity, event and media tools on `noesis-knowledge-engine`; source pack `config/source_packs/osint.json` | Manifest v1 (`packs/osint/pack.json`); source-pack semver + immutable versions |
| Science / Research | `src/domains/research/*` (enrichers, paper families, OpenReview rounds); `src/kb/methodology_provenance.py`, `systematic_reviews.py`, `citation_preservation.py`, `cultural.py`, `mathematics.py`, `patents.py` | `documents` + revisions; methodology, paper-family, review, citation-snapshot, cultural, mathematics and patent tables owned by those modules | `noesis-research` (3 tools; the only server the catalog attributes to a pack); research tools on `noesis-knowledge-engine`; source packs `research.json`, `scientific.json` | `DomainPack` (code-registered `research`) + manifest v1 (`packs/science/pack.json`); source-pack semver |
| Geospatial | `src/kb/geospatial.py` (places, geometry, resolution, spatial relations); `src/kb/geospatial_features.py` (feature revisions, snapshots, projection); `src/ingestion/wfs_api.py`, `geojson_features.py`, `transit_sources.py`; `src/kb/transit.py` | `geospatial_places` / `_place_revisions` / `_geometries`; `geospatial_features` / `_feature_revisions` / `_feature_snapshots`; `spatial_receipts`; `transit_*` | 26 tools on `noesis-knowledge-engine` (geospatial, spatial, transit); source pack `config/source_packs/geospatial.json` | Manifest v1 (`packs/geospatial/pack.json`); source-pack semver |
| Source packs | `src/ingestion/source_packs.py` (validation, install, enablement), `source_pack_runtime.py` (runs, cursors, budgets, schedules, projectors), `source_pack_upgrades.py` (impact, apply) | `source_pack_versions`, `source_pack_current`, `source_pack_runs`, `source_pack_source_runs`, `source_pack_checkpoints`, `source_pack_watermarks`, `source_pack_schedules`, `source_pack_license_acceptance`, `source_pack_quarantine`, `source_pack_upgrade_receipts` | 20 `*source_pack*` tools on `noesis-knowledge-engine`; maintenance orchestrator (`src/kb/maintenance.py`) | Semver with prerelease/build suffix; immutable `(pack_id, version)`; no downgrade; semantic upgrade diff |
| Intake | `src/kb/intake_modes.py` and `src/kb/intake_*.py`; `src/kb/investigation_templates.py`; `src/kb/research_recipes.py`; `src/kb/research_projects.py`; `src/kb/decisions.py`; `src/kb/authored_reports.py` | `intake_sessions` + revisions/commands; `investigation_templates` + revisions; `research_recipe_revisions` / `_runs` / `_checkpoints`; `research_projects` + revisions; `research_decisions` + revisions; `authored_reports` + revisions | 104 intake tools and 32 recipe/template/project tools on `noesis-knowledge-engine` | Revision counters per object; templates pin source packs by `{pack_id, version}` |

## C01.1 — Capability strings and their providers

Capability strings are declared in two places: `DomainPack.capabilities` for
code-registered bundles (`src/domains/*/pack.py`) and `PackManifest.capabilities`
in `packs/*/pack.json`. Neither is resolved against code today: the strings are
metadata that `install_manifest` copies into a synthesized `DomainPack`.

| Declaring bundle(s) | Capability strings | Provider code | Exposing server / tools |
| --- | --- | --- | --- |
| `packs/osint` | bounded-public-osint-acquisition | `src/ingestion/source_pack_runtime.py` over `config/source_packs/osint.json` | `run_source_pack_execution` and related (`noesis-knowledge-engine`) |
| `packs/osint` | probable-origin-graph, origin-aware-corroboration, contradiction-scan, source-reliability-cards, cited-timeline-reconstruction, media-provenance-and-reuse, investigation-audit | `src/osint/independence.py`, `corroboration.py`, `contradictions.py`, `reliability.py`, `timeline.py`, `provenance.py`, `investigations.py` | `noesis-osint`: evidence_origin_graph, origin_signals, corroborate, contradiction_scan, source_reliability, timeline_reconstruct, image_provenance, image_reuse, image_reuse_findings, trace_artifact, investigation_audit |
| `packs/osint` | source-identity-and-relationships | `src/kb/source_identity.py` | source-identity tools on `noesis-knowledge-engine` |
| `packs/science`, code `research` (none declared) | bounded-scholarly-source-acquisition | source-pack runtime over `research.json`, `scientific.json`; `src/ingestion/connectors/paper/*` | source-pack tools; `noesis-research` (citation_graph, literature_claims, venues) |
| `packs/science` | study-methodology-records, study-replication-graph | `src/kb/methodology_provenance.py` (`MethodologyStore`) | register/get/search methodology tools, `get_study_replication_graph` |
| `packs/science` | paper-family-versions, openreview-round-comparison | `src/domains/research/paper_families.py`, `openreview_rounds.py` | paper-family and OpenReview tools |
| `packs/science` | systematic-review-screening | `src/kb/systematic_reviews.py`, `systematic_review_decisions.py` | review protocol/candidate/screening tools |
| `packs/science` | preserved-citation-snapshots | `src/kb/citation_preservation.py` | citation snapshot tools |
| `packs/science` | cultural-primary-source-records, rights-gated-cultural-assets, primary-source-research-links, cultural-object-place-projection | `src/kb/cultural.py`, `src/ingestion/cultural_sources.py` (place projection writes through `src/kb/geospatial.py`) | cultural tools |
| `packs/science` | math-literature-records, oeis-sequence-records, formal-library-snapshots, formal-proof-dependencies, formal-revision-comparison, math-source-objects, math-cross-source-links | `src/kb/mathematics.py`, `src/ingestion/math_sources.py` | mathematics tools |
| `packs/geospatial` | native-wfs-2.0.0-geojson-acquisition, bounded-geojson-featurecollection-import | `src/ingestion/wfs_api.py`, `geojson_features.py` | source-pack tools, `import_geospatial_features` |
| `packs/geospatial` | provenance-preserving-feature-revisions, durable-spatial-projection, complete-snapshot-removal-semantics, points-inside-boundary-query, reviewable-boundary-name-resolution | `src/kb/geospatial_features.py`, `src/kb/geospatial.py` | geospatial feature/place tools |
| `packs/legal`, code `legal` | cellar-work-expression-manifestation, federal-court-decisions, berlin-legal-publications, as-of-version-selection, cited-passage-retrieval, version-comparison (both); explicit-citation-graph, legislative-dossier-links, gated-retrieval-modes (manifest only) | `src/kb/legal.py`, `legal_retrieval.py`, `src/ingestion/legal_sources.py` | legal tools |
| `packs/products` | nine product capabilities | `src/kb/products.py`, `src/ingestion/product_sources.py` | product tools |
| `packs/economics`, code `economics` | six economic capabilities (identical lists) | `src/domains/economic/*` | `kb_economic`, economic release tools |
| `packs/political`, code `political` | five political capabilities (identical lists) | `src/domains/political/*` | `kb_political`, legislative dossier tools |
| `packs/technology`, code `technology` | six technical capabilities (identical lists) | `src/domains/technical/*` | `kb_technical`, technical inventory/impact tools |
| `packs/market` | eight market capabilities | `src/domains/market/*` | `noesis-market` (62 tools) |
| code `news`, `research`; `packs/energy` | none declared | news: `src/domains/news/*` enrichers and routes; energy: keyword enricher only | news routes; none |

**Duplicates.** Economics, political, technology and legal declare their
capabilities twice, in code and in a manifest. The lists are identical except
legal, whose manifest adds three strings the code pack omits.

**Orphans.** No declared string lacks an implementation. Implementations
without a declared string: news (enrichers and routes), research (enrichers;
its behavior is declared only on `packs/science`), energy (one keyword
enricher), patents, LEI, standards and transit (delivered as source expansions
into existing packs without new capability strings), and the intake,
recipe and project surfaces (not packs).

**Hard-coded catalog mapping.** `_server_pack` attributes exactly one server to
a pack (`research_mcp` → `research`). Every other server has `pack: null`, so
disabling a pack never marks its tools `disabled`. `_required_data` is keyed
by server stem and tool name; only `market_mcp` distinguishes stores per tool.

## C01.2 — Authoritative stores per record type

| Record type | Authoritative store | ID scheme | Revision addressability | Native-revision link | Namespace scoping |
| --- | --- | --- | --- | --- | --- |
| Places | `src/kb/geospatial.py`: `geospatial_places`, `geospatial_place_revisions`, `geospatial_place_current` | `place_id` | Immutable place revisions | Evidence/source JSON on the revision | Yes (`namespace`, with `global`) |
| Geometries | `src/kb/geospatial.py`: `geospatial_geometries` | `geometry_id` (content-derived) | Immutable rows; validity interval | `source_json` (for example GTFS stop, cultural object) | Yes |
| Spatial features | `src/kb/geospatial_features.py`: `geospatial_features`, `_feature_revisions`, `_feature_snapshots` | feature id per collection | Immutable feature revisions and snapshots | Source-pack run and page (`geospatial_feature_pages`) | Yes |
| Documents | `src/ingestion/document_store.py` (`documents`, content blobs) with `src/ingestion/revisions.py` (`document_revision_records`, `document_current_revisions`) | `document_id`; `revision_id` | Immutable revision records with committed watermark | `pack_id` + `source_pack_version` in revision metadata | Via KB domain membership and namespaces, not per row |
| Evidence | Evidence is not a separate store: it is documents + revisions + claims, packaged by `src/evidence_bundle/builder.py` (`noesis-evidence-bundle-v1`) | bundle hash | Bundles are hashed exports | References document revisions | Inherits from the referenced records |
| Claims | `argument_claims` (warehouse seed; `src/argument_mining`) | `claim_id` | Not revisioned | `document_id` | No |
| Entities | `src/kb/entities.py` (`canonical_entities`, `entity_aliases`) with history in `src/kb/entity_history.py` | entity id | Identity decisions, redirects and publications are recorded | Assignments to documents | No |
| Temporal records | `src/kb/temporal.py`: `kb_temporal_assertions`, `kb_document_times` | assertion id | Bitemporal (valid time, system time); retractions kept | Source document/evidence per assertion | Per domain |
| Source identities | `src/kb/source_identity.py`: `source_identities` + revisions + relationships | source identity id | Immutable revisions | Evidence per revision | Yes |
| Intake sessions | `src/kb/intake_modes.py`: `intake_sessions`, `intake_session_revisions`, `intake_session_commands` | `session_id` | Revision counter; commands idempotent by `command_key` | References existing artifacts | Yes |
| Research projects | `src/kb/research_projects.py`: `research_projects` + revisions, expenditures, reservations | `project_id` | Revision counter | Origin pins (`source_packs`) | Yes |
| Decisions | `src/kb/decisions.py`: `research_decisions` + revisions | `decision_id` | Revision counter | Evidence commands | Yes |
| Reports | `src/kb/authored_reports.py`: `authored_reports` + revisions | `report_id` | Revision counter | Assertion dependencies pin document revisions | Yes |
| Investigation templates | `src/kb/investigation_templates.py` + revisions | `template_id` | Revision counter | Pins `{pack_id, version}` | Yes |
| Recipes and runs | `src/kb/research_recipes.py`: recipe revisions, runs, checkpoints | recipe id + revision; `run_id` | Recipe revisions; run receipts | Run records `execution_mode`, `actions_executed` | Yes |
| Source-pack versions | `src/ingestion/source_packs.py`: `source_pack_versions`, `source_pack_current` | `(pack_id, version)` | Immutable versions, content-hashed manifest | n/a | Deployment-wide |
| Cursors, runs, receipts | `src/ingestion/source_pack_runtime.py`: checkpoints, runs, source runs, watermarks | `run_id`; `(pack_id, source_id)` | Run receipts hashed; checkpoints tied to `manifest_hash` | n/a | Deployment-wide |
| Schedules | `source_pack_schedules` (one row per pack) | `pack_id` | Mutable; audited via maintenance | n/a | Deployment-wide |
| Domain-pack registrations | `src/domains/registry.py` (`_REGISTRY`, `_ENABLED`, process memory) and `src/domains/pack_install.py` (`_INSTALLED`) | pack name | None (process-local) | n/a | None |
| Published pack manifests | `src/domains/pack_registry.py` (filesystem `<root>/<name>/<version>/pack.json`) | `(name, version)` | Immutable unless `force` | n/a | None |
| Modulo planning and notes | Modulo (external) | Modulo record id + version | Modulo's own | Referenced by intake handoffs | Modulo workspace |

Modulo-owned planning and note state is referenced by intake handoffs
(`export_modulo_intake_handoff`, migration previews) and never migrated into
Noesis; composition must keep it that way.

## C01.3 — Version and dependency rules

| Surface | Version syntax | Range syntax | Comparison / resolution | Compatibility rule | Enforcement point |
| --- | --- | --- | --- | --- | --- |
| Schema registry (`src/kb/schema_registry.py`) | Strict `MAJOR.MINOR.PATCH` (`SEMVER_RE`, no leading zeros, no suffix) | exact, `*`/`latest`/empty, `^x.y.z` (caret, 0.x-aware), `~x.y.z` (tilde), comma-joined `>=,<=,>,<,=` clauses | `resolve()` picks the highest satisfying active module, ties broken by `content_hash` | Per-module `compatibility_policy` (`none`/`backward`/`full`); `compare_content` classifies compatible/breaking/ambiguous; breaking registration refused under `backward` | `register()`, `resolve()` |
| Schema dependencies | — | — | `declare_dependency(module_id, consumer_kind, consumer_id)`; consumer kinds include `pack` and `tool` | `impact()` lists consumers of a module | `declare_dependency`, `impact` |
| Ontology (`src/kb/ontology.py`) | Delegates to the schema registry (`kind: ontology` / `crosswalk`) | Same as schema registry | Same | Crosswalk mapping kinds equivalent/broader/narrower/related/incompatible; incompatible pairs block expansion | `publish`, `register_crosswalk`, `expand` |
| Source packs (`src/ingestion/source_packs.py`) | Semver with optional `-prerelease` / `+build` suffix (`_SEMVER`) | None: exact versions only | Tuple compare on `(major, minor, patch)`, suffix ignored for ordering | Same version with different content refused (`immutable_version`); downgrade refused; `_semantic_upgrade_diff` reports domain and per-source capability changes | `install`, `preview_upgrade`, upgrade `apply` |
| Source upgrades (`source_pack_upgrades.py`) | as source packs | — | Preview hash + impact hash must match at apply | Impact lists templates, projects, schedules, reports and their retained-pin status | `preview_impact`, `apply` |
| Pack registry (`src/domains/pack_registry.py`) | `major.minor.patch` | None (latest or exact) | `_version_key`: integer tuple; unparseable parts sort low | Publishing an existing version refused unless `force` | `publish`, `latest_version` |
| Manifest v1 (`src/domains/pack_format.py`) | `VERSION_RE` `\d+\.\d+\.\d+` (leading zeros allowed) | `schema_versions` values are exact versions, not ranges | — | `validate_manifest` ignores unknown top-level keys; `from_dict` silently drops them and coerces types | `validate_manifest`, `load_manifest` |

**Divergences C02/C03 must reconcile.**

1. Three semver grammars: schema registry (strict), source packs (suffixes
   allowed), manifest v1 and pack registry (leading zeros allowed).
2. Only the schema registry has ranges. Source packs and packs use exact
   versions, and templates pin exact source-pack versions.
3. Manifest v1 silently drops unknown fields. The current `packs/*/pack.json`
   files already carry `source_pack`, `query_examples` and `exclusions`, which
   `PackManifest` discards.
4. Tie-breaking differs: the schema registry uses `content_hash`; the pack
   registry has no tie (versions are unique directories).
5. Only the schema registry and source packs have content hashes; pack
   manifests have none.

**Recommendation.** The resolver adopts the schema registry's rules: strict
semver for identities, `_satisfies` range syntax for requirements, highest
satisfying version, and `content_hash` for tie-breaking and locking. Source-pack
versions keep their own grammar and immutability rules and are referenced by
exact `(pack_id, version, manifest_hash)`, because the source-pack store stays
authoritative for them. Manifest v1 is read through an adapter that preserves
unknown fields as recorded extensions rather than dropping them.

## C01.4 — Routes, tool IDs, aliases, and data prerequisites

Tool IDs are `"<server>.<tool>"` (for example `noesis-osint.corroborate`) and
must be emitted unchanged. Server aliases come from `LEGACY_SERVER_ALIASES` and
`.mcp.json` `compatibilityAliases`.

| Surface | HTTP routes | Server / tools | Aliases | Required data today |
| --- | --- | --- | --- | --- |
| OSINT | none via `DomainPack.routes` | `noesis-osint` (13 tools); source-identity, event, media tools on `noesis-knowledge-engine` | `neuronews-osint` → `noesis-osint` | `warehouse` for all 13 OSINT tools; `knowledge-engine-runtime` for the others |
| Research | none via `DomainPack.routes` (code pack has no route modules) | `noesis-research` (citation_graph, literature_claims, venues); research tools on `noesis-knowledge-engine` | `neuronews-research` → `noesis-research` | `warehouse` |
| Geospatial | none via `DomainPack.routes` | 26 tools on `noesis-knowledge-engine`: calculate_spatial_relation, get_geospatial_place, import_geospatial_features, inspect_geospatial_feature, inspect_geospatial_feature_coverage, inspect_geospatial_pack_readiness, list_geospatial_geometries, query_geospatial_event_map, query_geospatial_features_within, record_geospatial_resolution, register_geospatial_place, replay_geospatial_feature_query, replay_spatial_relation, resolve_geospatial_boundary, resolve_geospatial_candidates, retry_geospatial_feature_projection, review_geospatial_resolution, revise_geospatial_place, search_geospatial_knowledge, simplify_geospatial_geometry, store_geospatial_geometry, transit_departures, transit_feed_versions, transit_source_contracts, transit_stops_in_bbox | none | `knowledge-engine-runtime` |
| Sources | none | 20 source-pack tools on `noesis-knowledge-engine` | none | `knowledge-engine-runtime` |
| Intake | none | 104 intake tools and 32 recipe/template/project tools on `noesis-knowledge-engine` | none | `knowledge-engine-runtime`; some recipe/loop tools list scope strings (`knowledge:projects:read`, …) in `required_data` |
| News | `news_routes`, `article_routes`, `sentiment_routes`, `sentiment_trends_routes`, `event_routes`, `event_timeline_routes`, `veracity_routes` (from `DomainPack.route_modules`) | pipeline tools | `neuronews-pipeline` | `warehouse` |
| Domain packs | — | `noesis-domain-packs` (list_packs, pack_status, enable_pack, disable_pack, get_ui_flags, run_enrichers) | `neuronews-domain-packs` | `pack-registry` |

**Catalog state values.** `_state` reports `available`, `degraded`, `empty`,
`disabled`, `unauthorized` and `unavailable`, in that priority. Authorization is
checked first; a pack is `disabled` only through `_server_pack`, which today
covers `research_mcp` alone. `_data_state` probes concrete tables for the market
stores and the warehouse row count for `warehouse`-style prerequisites, and
reports `available` for every other prerequisite without probing.

**Preserved-identifier list.** Every `id` in
`contracts/generated/noesis-mcp-catalog-v1.json` (1,034 tool IDs across 26
Noesis servers) plus every server alias listed there. C04 tests the composed
catalog against this artifact: the same IDs, aliases, `required_scopes`,
`mutability` and `input_schema` must be emitted, and only the added
composition fields may differ.

**Tools exposed by several bundles.** `query_geospatial_event_map` serves OSINT
event mapping and Geospatial. Source-pack tools serve every bundle with a source
pack. The catalog attributes all of these to no pack.

## Unknowns

| Unknown | Why it matters | Decision needed |
| --- | --- | --- |
| Pack naming mismatch: code packs register as `economics` and `technology`, manifests use `economics` and `technology`, but the catalog lists `economic` and `technical` (directory names) and `config/domain_packs.json` enables by registered name | A composition keyed by name would see two identities for one bundle | C02 adapter maps directory names to registered names explicitly; C09.2 retires the directory-name listing |
| Duplicate declarations (economics, political, technology, legal exist both as code `DomainPack` and as `packs/` manifest) | Two contribution sources for one bundle | C02.2 adapter merges them into one composition manifest per bundle, with the manifest as the declaration source and code as the provider |
| Research vs Science: code pack `research` vs manifest `science` | Same bundle under two names | Treat `science` (manifest) as the bundle and `research` as its code provider; record an alias in C02.2 |
| Claims have no revisions and no namespace | Cross-pack references cannot pin a claim revision | Composition references claims by `claim_id` with revision addressing marked `unsupported` (allowed by the architecture) |
| Entities have no namespace scoping | Identity sharing is bounded by namespace in the architecture | Entity references carry the referencing record's namespace; no entity sharing across namespaces is inferred |
| Evidence has no single store | "Evidence" in the architecture is a role, not a table | Evidence references point at document revisions and claims; bundles stay exports |
| `research-discovery` pack name vs `science` bundle | `packs/science` points at `scientific.json`, while mathematics, patents and crossref live in `research.json` | C08.1/C09.3 record both source packs as Science/Research dependencies |

## Deferred decisions settled

The activation-journal storage and the startup reconciliation boundary are
settled in [ADR-003](decisions/ADR-003-composition-activation-journal.md).
