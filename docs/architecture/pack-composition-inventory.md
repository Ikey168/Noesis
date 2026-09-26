# Pack composition inventory (C01)

Status: inventory for slice C01 of the
[pack and workflow composition architecture](pack-workflow-composition.md),
tracked in [#1789](https://github.com/Ikey168/Noesis/issues/1789) and its
sub-issues C01.1 to C01.6. Later slices cite this document instead of
re-deriving ownership. Facts that could not be settled from the code are listed
under [Unknowns](#unknowns) with the decision each needs.

Machine-readable companions:

- `tests/fixtures/composition/capability_map.json` holds the capability map
  below (C01.1).
- `tests/fixtures/composition/preserved_identifiers.json` holds the preserved
  identifiers (C01.4). `scripts/generate_composition_identifiers.py --check`
  verifies it against the current catalog.
- [ADR-003](decisions/ADR-003-composition-journal-storage.md) records the
  journal storage and reconciliation decision (C01.5).

## Two pack systems exist today

| System | Where | Unit | Enablement | Notes |
| --- | --- | --- | --- | --- |
| Code-registered domain packs | `src/domains/<module>/` calling `register_pack` | Registered names: `news`, `research`, `economics`, `legal`, `political`, `technology` | Process-local `_ENABLED` set loaded from `config/domain_packs.json` or `NOESIS_ENABLED_PACKS` | Enrichers, route modules, UI flags, capability strings. `src/domains/market` ships helpers but registers no `DomainPack`. Module names `economic` and `technical` register as `economics` and `technology`. |
| Distributable manifests | `packs/<name>/pack.json` (`noesis-pack-v1`) | `economics`, `energy`, `geospatial`, `legal`, `market`, `osint`, `political`, `products`, `science`, `technology` | `install_manifest` registers and enables a synthesized `DomainPack` in the same registry | Also carry `source_pack`, `query_examples` and `exclusions`, which v1 validation ignores. |

Both systems share one enabled-state ledger (`src/domains/registry.py`).
Neither resolves a capability string to a provider, dependency or contract.

## C01.1 Capability strings and the code that provides them

All 77 declared strings have provider code. Tool IDs are
verified against the generated catalog; server `noesis-knowledge-engine` is
abbreviated in the tool column by tool name only.

| Capability string | Provider code | Exposing server / tools | Declaring bundles |
| --- | --- | --- | --- |
| `as-of-version-selection` | `src/kb/legal.py` | `noesis-knowledge-engine`: `select_legal_version_as_of`, `legal_selection_outcomes` | packs/legal, src/domains/legal |
| `berlin-legal-publications` | `src/ingestion/legal_sources.py`, `src/kb/legal.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+4 more) | packs/legal, src/domains/legal |
| `bitemporal-economic-observations` | `src/domains/economic/model.py`, `src/kb/temporal.py`, `src/domains/economic/releases.py` | `noesis-kb`, `noesis-knowledge-engine`: `kb_economic`, `kb_temporal`, `create_economic_release_snapshot` | packs/economics, src/domains/economics |
| `bitemporal-office-terms` | `src/domains/political/model.py`, `src/domains/political/queries.py` | `noesis-kb`: `kb_political` | packs/political, src/domains/political |
| `bounded-geojson-featurecollection-import` | `src/ingestion/geojson_features.py`, `src/kb/geospatial_features.py` | `noesis-knowledge-engine`: `import_geospatial_features` | packs/geospatial |
| `bounded-public-osint-acquisition` | `src/ingestion/source_pack_runtime.py`, `src/ingestion/source_packs.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+1 more) | packs/osint |
| `bounded-scholarly-source-acquisition` | `src/ingestion/source_pack_runtime.py`, `src/ingestion/scholarly_api.py`, `src/ingestion/europepmc_api.py`, `src/ingestion/datacite_api.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+1 more) | packs/science |
| `canonical-economic-indicators` | `src/domains/economic/model.py`, `src/domains/economic/queries.py`, `src/kb/contract.py` | `noesis-kb`: `kb_economic` | packs/economics, src/domains/economics |
| `canonical-package-coordinates` | `src/domains/technical/model.py`, `src/domains/technical/registries.py` | `noesis-kb`, `noesis-pipeline`: `kb_technical`, `run_connector` | packs/technology, src/domains/technology |
| `cellar-work-expression-manifestation` | `src/ingestion/legal_sources.py`, `src/kb/legal.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+4 more) | packs/legal, src/domains/legal |
| `cited-economic-comparisons` | `src/domains/economic/queries.py`, `src/domains/economic/releases.py` | `noesis-kb`, `noesis-knowledge-engine`: `kb_economic`, `compare_economic_release_snapshots`, `inspect_economic_release_comparison`, `export_economic_release_comparison` (+1 more) | packs/economics, src/domains/economics |
| `cited-passage-retrieval` | `src/kb/legal.py` | `noesis-knowledge-engine`: `get_legal_passages` | packs/legal, src/domains/legal |
| `cited-timeline-reconstruction` | `src/osint/timeline.py` | `noesis-osint`: `timeline_reconstruct` | packs/osint |
| `claim-observation-links` | `src/domains/economic/model.py`, `src/domains/economic/queries.py`, `src/analytics/claim_check.py` | `noesis-kb`, `noesis-statistics`: `kb_economic`, `claim_vs_data` | packs/economics, src/domains/economics |
| `company-dossiers-and-industry-models` | `src/domains/market/research.py`, `src/domains/market/dossier_inputs.py`, `src/domains/market/dashboard.py` | `noesis-market`: `build_market_company_dossier`, `build_market_industry_model`, `calculate_market_sizing`, `record_market_driver_hypotheses` (+3 more) | packs/market |
| `complete-snapshot-removal-semantics` | `src/kb/geospatial_features.py` | `noesis-knowledge-engine`: `run_source_pack_execution`, `inspect_geospatial_feature_coverage` | packs/geospatial |
| `contradiction-scan` | `src/osint/contradictions.py` | `noesis-osint`: `contradiction_scan` | packs/osint |
| `cultural-object-place-projection` | `src/kb/cultural.py`, `src/kb/geospatial.py` | `noesis-knowledge-engine`: `search_cultural_objects`, `inspect_cultural_object` | packs/science |
| `cultural-primary-source-records` | `src/ingestion/cultural_sources.py`, `src/kb/cultural.py` | `noesis-knowledge-engine`: `search_cultural_objects`, `inspect_cultural_object`, `cultural_source_contracts`, `cultural_readiness` (+2 more) | packs/science |
| `cutoff-bounded-price-history` | `src/domains/market/prices.py`, `src/domains/market/metrics.py`, `src/domains/market/actions.py` | `noesis-market`: `get_market_price_history`, `calculate_market_price_metrics` | packs/market |
| `dependency-compatibility-graph` | `src/domains/technical/queries.py`, `src/domains/technical/model.py` | `noesis-kb`: `kb_technical` | packs/technology, src/domains/technology |
| `durable-spatial-projection` | `src/kb/geospatial_features.py`, `src/kb/geospatial.py` | `noesis-knowledge-engine`: `retry_geospatial_feature_projection`, `inspect_geospatial_feature`, `store_geospatial_geometry` | packs/geospatial |
| `evidence-linked-model-comparison` | `src/kb/products.py` | `noesis-knowledge-engine`: `compare_product_models` | packs/products |
| `explicit-citation-graph` | `src/kb/legal.py` | `noesis-knowledge-engine`: `inspect_legal_work`, `lookup_legal_work` | packs/legal |
| `explicit-measurement-dimensions` | `src/domains/economic/model.py`, `src/domains/economic/releases.py` | `noesis-kb`, `noesis-knowledge-engine`: `kb_economic`, `compare_economic_release_snapshots` | packs/economics, src/domains/economics |
| `explicit-withdrawal-refresh-semantics` | `src/kb/products.py` | `noesis-knowledge-engine`: `run_source_pack_execution`, `inspect_product_identity` | packs/products |
| `federal-court-decisions` | `src/ingestion/legal_sources.py`, `src/kb/legal.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+4 more) | packs/legal, src/domains/legal |
| `formal-library-snapshots` | `src/ingestion/math_sources.py`, `src/kb/mathematics.py` | `noesis-knowledge-engine`: `inspect_formal_declaration`, `export_formal_references`, `search_mathematics` | packs/science |
| `formal-proof-dependencies` | `src/kb/mathematics.py`, `src/ingestion/math_sources.py` | `noesis-knowledge-engine`: `formal_declaration_dependencies` | packs/science |
| `formal-revision-comparison` | `src/kb/mathematics.py` | `noesis-knowledge-engine`: `compare_formal_snapshots` | packs/science |
| `gated-retrieval-modes` | `src/kb/legal_retrieval.py` | `noesis-knowledge-engine`: `legal_retrieval_modes`, `legal_readiness` | packs/legal |
| `historical-universe-screening` | `src/domains/market/screeners.py` | `noesis-market`: `screen_market_universe`, `save_market_screener_query`, `inspect_market_screener_run`, `export_market_screener_run` | packs/market |
| `incremental-git-ingestion` | `src/domains/technical/git_connector.py` | `noesis-knowledge-engine`, `noesis-pipeline`: `run_connector`, `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution` (+2 more) | packs/technology, src/domains/technology |
| `investigation-audit` | `src/osint/investigations.py` | `noesis-osint`: `investigation_audit` | packs/osint |
| `legislative-dossier-links` | `src/kb/legal.py`, `src/domains/political/legislative_dossiers.py` | `noesis-knowledge-engine`: `link_legal_dossier`, `save_legislative_dossier`, `inspect_legislative_dossier`, `legislative_dossier_timeline` (+3 more) | packs/legal |
| `math-cross-source-links` | `src/kb/mathematics.py` | `noesis-knowledge-engine`: `math_links`, `propose_math_links`, `review_math_link` | packs/science |
| `math-literature-records` | `src/ingestion/math_sources.py`, `src/kb/mathematics.py` | `noesis-knowledge-engine`: `inspect_math_literature`, `search_mathematics`, `math_source_contracts` | packs/science |
| `math-source-objects` | `src/kb/mathematics.py` | `noesis-knowledge-engine`: `record_math_object`, `inspect_math_object`, `extract_math_text_objects` | packs/science |
| `media-provenance-and-reuse` | `src/analytics/image_reuse.py`, `src/kb/multimodal_evidence.py`, `src/osint/imagery_gated.py` | `noesis-knowledge-engine`, `noesis-osint`: `image_provenance`, `image_reuse`, `image_reuse_findings`, `inspect_media_provenance` (+2 more) | packs/osint |
| `native-eprel-public-api-acquisition` | `src/ingestion/product_sources.py`, `src/kb/products.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+2 more) | packs/products |
| `native-open-icecat-acquisition` | `src/ingestion/product_sources.py`, `src/kb/products.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+2 more) | packs/products |
| `native-wfs-2.0.0-geojson-acquisition` | `src/ingestion/wfs_api.py`, `src/ingestion/source_pack_runtime.py`, `src/kb/geospatial_features.py` | `noesis-knowledge-engine`: `install_source_pack`, `preflight_source_pack_run`, `run_source_pack_execution`, `inspect_source_pack_run` (+2 more) | packs/geospatial |
| `oeis-sequence-records` | `src/ingestion/math_sources.py`, `src/kb/mathematics.py` | `noesis-knowledge-engine`: `inspect_oeis_sequence`, `search_mathematics` | packs/science |
| `official-source-manifests` | `src/ingestion/connectors/political_official.py`, `src/domains/political/queries.py`, `src/domains/political/enrichers.py` | `noesis-kb`, `noesis-pipeline`: `kb_political`, `run_connector` | packs/political, src/domains/political |
| `openreview-round-comparison` | `src/domains/research/openreview_rounds.py`, `src/ingestion/openreview_api.py` | `noesis-knowledge-engine`: `save_openreview_round_set`, `inspect_openreview_round_set`, `inspect_openreview_round`, `compare_openreview_rounds` (+3 more) | packs/science |
| `origin-aware-corroboration` | `src/osint/corroboration.py`, `src/osint/independence.py` | `noesis-kb`, `noesis-osint`: `corroborate`, `kb_corroborate` | packs/osint |
| `osv-cve-advisories` | `src/domains/technical/advisories.py`, `src/domains/technical/model.py` | `noesis-kb`, `noesis-knowledge-engine`: `kb_technical`, `assess_technical_inventory_impact` | packs/technology, src/domains/technology |
| `paper-family-versions` | `src/domains/research/paper_families.py` | `noesis-knowledge-engine`: `create_paper_family`, `inspect_paper_family`, `add_paper_family_member`, `correct_paper_family_relation` (+7 more) | packs/science |
| `per-provider-readiness` | `src/kb/products.py`, `src/ingestion/provider_readiness.py` | `noesis-knowledge-engine`: `products_readiness`, `product_provider_contracts` | packs/products |
| `point-in-time-instrument-resolution` | `src/domains/market/instruments.py`, `src/domains/market/asof.py`, `src/domains/market/capabilities.py` | `noesis-market`: `lookup_market_instrument` | packs/market |
| `points-inside-boundary-query` | `src/kb/geospatial_features.py`, `src/kb/geospatial.py` | `noesis-knowledge-engine`: `query_geospatial_features_within`, `replay_geospatial_feature_query` | packs/geospatial |
| `political-research-queries` | `src/domains/political/queries.py`, `src/kb/contract.py` | `noesis-kb`: `kb_political` | packs/political, src/domains/political |
| `preserved-citation-snapshots` | `src/kb/citation_preservation.py` | `noesis-knowledge-engine`: `register_citation_archive_policy`, `get_citation_archive_policy`, `capture_citation_snapshot`, `get_citation_snapshot` (+7 more) | packs/science |
| `primary-source-research-links` | `src/kb/cultural.py` | `noesis-knowledge-engine`: `primary_sources_for_work`, `link_cultural_research`, `suggest_cultural_research_candidates`, `review_cultural_research_candidate` | packs/science |
| `probable-origin-graph` | `src/osint/independence.py` | `noesis-osint`: `evidence_origin_graph`, `origin_signals` | packs/osint |
| `proposal-vote-lifecycle` | `src/domains/political/model.py`, `src/domains/political/queries.py` | `noesis-kb`: `kb_political` | packs/political, src/domains/political |
| `provenance-preserving-feature-revisions` | `src/kb/geospatial_features.py` | `noesis-knowledge-engine`: `inspect_geospatial_feature`, `inspect_geospatial_feature_coverage` | packs/geospatial |
| `provider-health-and-readiness` | `src/domains/market/operations.py`, `src/domains/market/capabilities.py` | `noesis-market`: `market_provider_health`, `market_readiness`, `evaluate_market_slos`, `record_market_operations_measurements` | packs/market |
| `provider-linked-document-acquisition` | `src/kb/products.py` | `noesis-knowledge-engine`: `acquire_product_documents`, `inspect_product_identity` | packs/products |
| `provider-scoped-product-identities` | `src/kb/products.py` | `noesis-knowledge-engine`: `lookup_product_models`, `inspect_product_identity`, `product_selection_outcomes` | packs/products |
| `quantitative-and-specialized-analytics` | `src/domains/market/quantitative.py`, `src/domains/market/specialized.py` | `noesis-market`: `run_market_event_study`, `run_market_factor_analysis`, `run_market_backtest`, `run_market_walk_forward` (+12 more) | packs/market |
| `release-vintage-history` | `src/domains/economic/releases.py`, `src/domains/economic/model.py`, `src/ingestion/connectors/dataset/queries.py` | `noesis-kb`, `noesis-knowledge-engine`, `noesis-market`, `noesis-statistics`: `kb_economic`, `get_observations`, `create_economic_release_snapshot`, `inspect_economic_release_snapshot` (+1 more) | packs/economics, src/domains/economics |
| `reviewable-boundary-name-resolution` | `src/kb/geospatial_features.py`, `src/kb/geospatial.py` | `noesis-knowledge-engine`: `resolve_geospatial_boundary`, `resolve_geospatial_candidates`, `record_geospatial_resolution`, `review_geospatial_resolution` | packs/geospatial |
| `reviewable-cross-provider-matching` | `src/kb/products.py` | `noesis-knowledge-engine`: `propose_product_matches`, `review_product_match` | packs/products |
| `rights-gated-cultural-assets` | `src/kb/cultural.py` | `noesis-knowledge-engine`: `cultural_rights_policy`, `acquire_cultural_assets` | packs/science |
| `scoped-entity-resolution` | `src/domains/political/model.py`, `src/kb/cross_domain.py` | `noesis-kb`: `kb_political` | packs/political, src/domains/political |
| `section-addressable-specifications` | `src/domains/technical/specifications.py`, `src/domains/technical/model.py` | `noesis-kb`, `noesis-pipeline`: `kb_technical`, `run_connector` | packs/technology, src/domains/technology |
| `source-identity-and-relationships` | `src/kb/source_identity.py` | `noesis-knowledge-engine`: `register_source_identity`, `lookup_source_identity`, `source_identity_history`, `revise_source_identity` (+9 more) | packs/osint |
| `source-reliability-cards` | `src/osint/reliability.py` | `noesis-osint`: `source_reliability` | packs/osint |
| `study-methodology-records` | `src/kb/methodology_provenance.py` | `noesis-knowledge-engine`: `register_methodology_study`, `get_methodology_study`, `search_methodology_studies`, `extract_methodology_statements` (+6 more) | packs/science |
| `study-replication-graph` | `src/kb/methodology_provenance.py` | `noesis-knowledge-engine`: `link_study_artifact`, `get_study_replication_graph` | packs/science |
| `systematic-review-screening` | `src/kb/systematic_reviews.py`, `src/kb/systematic_review_decisions.py` | `noesis-knowledge-engine`: `create_review_protocol`, `inspect_review_protocol`, `amend_review_protocol`, `add_review_candidate` (+9 more) | packs/science |
| `temporal-technical-queries` | `src/domains/technical/queries.py`, `src/kb/contract.py` | `noesis-kb`: `kb_technical` | packs/technology, src/domains/technology |
| `thesis-and-alert-receipts` | `src/domains/market/research.py`, `src/domains/market/alerts.py`, `src/domains/market/delivery.py` | `noesis-market`: `save_market_thesis`, `review_market_thesis`, `save_market_alert_watch`, `run_market_alerts` (+4 more) | packs/market |
| `unit-normalized-display-attributes` | `src/kb/products.py`, `src/kb/quantitative.py` | `noesis-knowledge-engine`: `inspect_product_identity`, `compare_product_models` | packs/products |
| `version-comparison` | `src/kb/legal.py` | `noesis-knowledge-engine`: `compare_legal_versions` | packs/legal, src/domains/legal |
| `versioned-filing-facts` | `src/domains/market/financial_facts.py`, `src/domains/market/metrics.py` | `noesis-market`: `get_market_financial_statements`, `calculate_market_fact_metrics` | packs/market |

**Duplicated strings.** 23 strings are declared twice, once by a
`packs/` manifest and once by its code-registered twin: legal (6), technology
(6), economics (6) and political (5). Composition keeps one declaration per
bundle; the twins are adapted as one bundle in C09.2.

**Orphaned strings.** None: every string has provider code. Some strings have
no tool of their own and happen only inside source-pack execution or other
tools: `complete-snapshot-removal-semantics`,
`explicit-withdrawal-refresh-semantics`, `explicit-citation-graph`,
`incremental-git-ingestion` and `section-addressable-specifications`.

**Implemented but unreachable.** No caller was found for these, so they are
not bindable until a tool or ingestion path reaches them:

- Political alias resolution (`resolve_alias`, `record_alias` in
  `src/domains/political/model.py`) is reached only from `load_fixture`.
- OSV/CVE advisory ingestion (`ingest_advisory` in
  `src/domains/technical/advisories.py`) has no caller outside its module.
- Economic series registration and claim links are reached only from
  `load_fixture`.
- The OSINT source pack's `policy` block (`review_gate`,
  `deny_person_identification`) is not read anywhere in `src/`. OSINT
  acquisition runs through the generic declarative runtime.

**Mismatched source-pack references.** The science manifest names
`config/source_packs/scientific.json`, but its mathematics strings are served
from `research.json` and OpenReview from `openreview.json`. The geospatial
source pack also carries a GTFS feed that no capability string names.

**Implementations without a declared string.**

- **research literature analytics**: `src/domains/research/analytics.py`, `src/ingestion/opencitations.py` ; `noesis-research.citation_graph`, `noesis-research.literature_claims`, `noesis-research.venues` — Science pack query_examples use citation_graph but no science capability string names it; catalog gates research_mcp on the "research" domain pack.
- **osint entity dossier and relationship paths**: `src/osint/dossier.py`, `src/osint/paths.py` ; `noesis-osint.entity_dossier`, `noesis-osint.relationship_path` 
- **osint artifact provenance chain**: `src/osint/provenance.py` ; `noesis-osint.trace_artifact` 
- **osint gated geolocation/coordination**: `src/osint/gated.py`, `src/osint/imagery_gated.py` ; no catalog tool — geolocate_claims, narrative_coordination, reverse_image_search, geolocate_image are env-gated and absent from the generated catalog.
- **GTFS transit schedules**: `src/kb/transit.py`, `src/ingestion/transit_sources.py` ; `noesis-knowledge-engine.transit_source_contracts`, `noesis-knowledge-engine.transit_feed_versions`, `noesis-knowledge-engine.transit_departures`, `noesis-knowledge-engine.transit_stops_in_bbox` — GTFS source is in config/source_packs/geospatial.json but the geospatial pack declares no transit string.
- **geospatial places, geometries and spatial relations**: `src/kb/geospatial.py` ; `noesis-knowledge-engine.register_geospatial_place`, `noesis-knowledge-engine.revise_geospatial_place`, `noesis-knowledge-engine.get_geospatial_place`, `noesis-knowledge-engine.store_geospatial_geometry`, `noesis-knowledge-engine.list_geospatial_geometries` — Used in geospatial query_examples (search_geospatial_knowledge, calculate_spatial_relation) with no matching string.
- **pipeline geo map**: `src/analytics/geospatial.py` ; `noesis-pipeline.geo_map` 
- **patents (EPO OPS)**: `src/kb/patents.py`, `src/ingestion/patent_sources.py` ; `noesis-knowledge-engine.patent_source_contracts`, `noesis-knowledge-engine.inspect_patent_publication`, `noesis-knowledge-engine.patent_family_members`, `noesis-knowledge-engine.patent_links`, `noesis-knowledge-engine.propose_patent_link` — epo-ops source in config/source_packs/research.json.
- **LEI company identities (GLEIF)**: `src/kb/lei.py`, `src/ingestion/lei_sources.py` ; `noesis-knowledge-engine.company_source_contracts`, `noesis-knowledge-engine.inspect_lei_entity`, `noesis-knowledge-engine.lei_parents_as_of`, `noesis-knowledge-engine.propose_lei_registry_links`, `noesis-knowledge-engine.link_company_identity` — gleif source in config/source_packs/economic.json.
- **standards editions and certificates (ISO)**: `src/kb/standards.py`, `src/ingestion/standards_sources.py` ; `noesis-knowledge-engine.standards_source_contracts`, `noesis-knowledge-engine.inspect_standard_edition`, `noesis-knowledge-engine.inspect_certificate`, `noesis-knowledge-engine.import_certificates`, `noesis-knowledge-engine.propose_certificate_product_links` — iso-open-data source in config/source_packs/technical.json.
- **technical inventory impact reports**: `src/domains/technical/inventory.py`, `src/domains/technical/impact.py`, `src/domains/technical/impact_reports.py` ; `noesis-knowledge-engine.import_technical_inventory`, `noesis-knowledge-engine.inspect_technical_inventory`, `noesis-knowledge-engine.assess_technical_inventory_impact`, `noesis-knowledge-engine.create_technical_impact_report`, `noesis-knowledge-engine.inspect_technical_impact_report` 
- **funding opportunities bundle**: `src/kb/funding_opportunities.py`, `src/kb/funding_eligibility.py`, `src/kb/funding_workspaces.py`, `src/ingestion/funding_providers.py` ; `noesis-knowledge-engine.funding_provider_contracts`, `noesis-knowledge-engine.acquire_funding_source`, `noesis-knowledge-engine.list_funding_opportunities`, `noesis-knowledge-engine.assess_funding_eligibility`, `noesis-knowledge-engine.build_funding_shortlist` 
- **quantitative metrics and units**: `src/kb/quantitative.py` ; `noesis-knowledge-engine.register_quantitative_unit`, `noesis-knowledge-engine.register_quantitative_metric`, `noesis-knowledge-engine.record_quantitative_observation`, `noesis-knowledge-engine.read_quantitative_series`, `noesis-knowledge-engine.assess_quantitative_comparability` 
- **event records and dossiers**: `src/kb/events.py`, `src/kb/event_dossiers.py` ; `noesis-knowledge-engine.create_event_record`, `noesis-knowledge-engine.get_event_record_as_of`, `noesis-knowledge-engine.event_timeline`, `noesis-knowledge-engine.relate_events`, `noesis-knowledge-engine.create_event_dossier` 
- **multilingual / cross-language claims**: `src/kb/cross_language.py` ; `noesis-knowledge-engine.record_language_text`, `noesis-knowledge-engine.record_translation`, `noesis-knowledge-engine.align_cross_language_claims`, `noesis-knowledge-engine.compare_cross_language_claims`, `noesis-knowledge-engine.multilingual_search` 
- **tabular dataset intelligence**: `src/kb/dataset_intelligence.py` ; `noesis-knowledge-engine.register_dataset_catalog`, `noesis-knowledge-engine.search_datasets`, `noesis-knowledge-engine.ingest_tabular_dataset`, `noesis-knowledge-engine.compare_dataset_releases`, `noesis-knowledge-engine.suggest_dataset_joins` 
- **Zotero library sync**: `src/ingestion/zotero_sync.py` ; `noesis-knowledge-engine.sync_zotero_library`, `noesis-knowledge-engine.list_zotero_items`, `noesis-knowledge-engine.inspect_zotero_item`, `noesis-knowledge-engine.export_zotero_bibliography` 
- **OpenCitations acquisition**: `src/ingestion/opencitations.py` ; `noesis-knowledge-engine.acquire_opencitations` 
- **market briefs and operations recovery**: `src/domains/market/research.py`, `src/domains/market/operations.py` ; `noesis-market.generate_market_brief`, `noesis-market.deliver_market_brief`, `noesis-market.export_market_brief`, `noesis-market.export_market_brief_evidence_bundle`, `noesis-market.schedule_market_brief` 
- **economic-market dashboard**: `src/domains/economic/dashboard.py`, `src/domains/market/dashboard.py` ; `noesis-market.get_economic_market_dashboard` 

**Catalog pack mapping.** `_server_pack` gates only `research_mcp`, to the
code-registered `research` pack. No `packs/` bundle gates any server, including
OSINT and market. `_required_data` has pack-specific branches only for
`market_mcp` (per-store labels such as `market-instrument-store` and
`economic-release-store`); every other tool gets a generic label such as
`knowledge-engine-runtime` or `warehouse`.

## C01.2 Authoritative stores

Each record type has exactly one owner. Composition references these records
through their owner and never copies them.

| Record type | Authoritative store | ID scheme | Revision addressability | Native-revision link | Namespace scoping |
| --- | --- | --- | --- | --- | --- |
| Places | `src/kb/geospatial.py` (`geospatial_places`, `geospatial_place_revisions`, `geospatial_place_current`) | `place:<digest>` | Immutable place revisions | Revisions record their source assertion | Yes, per namespace with a `global` fallback |
| Geometries | `src/kb/geospatial.py` (`geospatial_geometries`) | Content-addressed geometry ID | Immutable (content-addressed) | Projected geometries keep source CRS and the importing feature revision | Yes |
| Geocode resolutions and reviews | `src/kb/geospatial.py` (`geocode_resolutions`, `geocode_reviews`) | `geocode-review:<digest>` | Immutable review rows | Link to candidate places | Yes |
| Spatial results | `src/kb/geospatial.py` (`spatial_receipts`) | Receipt ID from request hash | Immutable, replayable | Pins input geometry IDs and algorithm | Yes |
| Acquired vector features | `src/kb/geospatial_features.py` (`geospatial_features`, `geospatial_feature_revisions`, `geospatial_feature_current`) | Feature key from provider, collection, native ID; `geofeature-rev:<digest>` | Immutable feature revisions | Each revision links its `document_id`, page provenance and projected geometry | Yes |
| Documents and evidence text | `src/ingestion/revisions.py` (`documents`, `document_revision_records`) | `document_id` plus `revision_id` | Immutable revisions with predecessors | Records `pack_id`, `run_id`, `source_id` and the source-pack version | Warehouse-wide; access through document scopes |
| Entities | `src/kb/entities.py`, `src/kb/entity_history.py` | Entity ID with identity history | Identity decisions are versioned | Mentions link to documents | Yes |
| Temporal event records | `src/kb/events.py` | `event:<digest>`, `event-account-revision:<digest>` | Immutable account revisions | Accounts cite documents | Yes |
| Intake sessions | `src/kb/intake_modes.py` (`intake_sessions`, `intake_session_revisions`) | `intake:<hash>` | Optimistic revisions; each revision retained | References carry an authoritative version | Owner plus namespace |
| Research projects | `src/kb/research_projects.py` | `project:<hash>` | Retained revisions | `template_origin.source_packs` pins versions and manifest hashes | Owner plus namespace |
| Investigation templates | `src/kb/investigation_templates.py` | `template:<hash>` | Retained revisions | Pins source packs by version | Owner plus namespace |
| Decisions | `src/kb/decisions.py` | `decision:<hash>` | Retained revisions | Evidence links | Owner plus namespace |
| Authored reports | `src/kb/authored_reports.py` | `report:<hash>` | Retained revisions | Assertions pin document revisions | Owner plus namespace |
| Recipes and recipe runs | `src/kb/research_recipes.py` | `research-recipe:<hash>`, `recipe-run:<digest>` | Content-hashed recipes; checkpointed runs | Receipts pin tool versions and snapshot tokens | Namespace |
| Source-pack versions, enablement, health | `src/ingestion/source_packs.py` | `(pack_id, version, manifest_hash)` | Immutable versions; no downgrade | Not applicable | Deployment-wide |
| Source runs, cursors, watermarks, schedules | `src/ingestion/source_pack_runtime.py` | `source-run:<digest>` from `(pack_id, run_key)`; one schedule row per `pack_id` | Receipts hashed; cursors current-only | Runs pin pack version and manifest hash | Deployment-wide |
| Source upgrade receipts | `src/ingestion/source_pack_upgrades.py` | `source-pack-upgrade:<hash>` | Immutable | Pins old and new manifest hashes | Deployment-wide |
| Schema modules and crosswalks | `src/kb/schema_registry.py`, `src/kb/ontology.py` | `kind:name@version:hash` | Immutable, content-addressed | Not applicable | Deployment-wide |
| OSINT observations and origins | `src/osint/` over warehouse tables | Document identities | Current-only; no revision store of its own | Through documents | Warehouse-wide |

**Modulo.** Modulo planning and note state is referenced from intake sessions
through workspace and plugin links carrying an authoritative version. It is not
migrated or copied by composition.

**Records with more than one plausible owner.** Place-like records in cultural
collections (`src/kb/cultural.py`) and transit stops (`src/kb/transit.py`) are
projected into the geospatial stores by their source-pack projectors but keep
domain records in their own tables. The decision, recorded for C09.3, is that
geometry and place identity belong to Geospatial and domain attributes belong
to the domain store.

## C01.3 Version and dependency rules

| Surface | Range syntax | Comparison rule | Compatibility rule | Enforcement point |
| --- | --- | --- | --- | --- |
| Schema registry (`src/kb/schema_registry.py`) | Exact `M.m.p`, `^`, `~`, comparator lists (`>=1.0.0,<2.0.0`); `*` and `latest` match anything | Strict semver tuple; `SEMVER_RE` forbids leading zeros | Per module `compatibility_policy` of `none`, `backward` or `full`; dependencies need `kind`, `name`, `version` and must resolve | `register`, `resolve`, `declare_dependency` |
| Ontology (`src/kb/ontology.py`) | Same as the schema registry (ontologies are registry modules) | Same | Crosswalk mapping kinds `equivalent`, `broader`, `narrower`, `related`, `incompatible`; `incompatible` pairs never expand | `publish`, `register_crosswalk`, `expand` |
| Source packs (`src/ingestion/source_packs.py`) | Exact versions only | `_version` semver tuple plus the raw string | No downgrade in place; a version is immutable per `manifest_hash`; upgrades go through a semantic diff | `install`, `preview_upgrade` |
| Source upgrades (`src/ingestion/source_pack_upgrades.py`) | Exact versions | As source packs | Impact preview over templates, projects, schedules and reports; apply requires matching preview and impact hashes | `preview_impact`, `apply` |
| Pack registry (`src/domains/pack_registry.py`) | Exact versions or latest | `_version_key`: integer parts, unparseable parts sort low | Versions immutable unless `force` | `publish`, `get` |
| Manifest v1 (`src/domains/pack_format.py`) | `VERSION_RE` `\d+.\d+.\d+` (allows leading zeros) | Not compared | None | `validate_manifest` rejects bad types and formats; `PackManifest.from_dict` silently coerces everything and drops unknown fields |

**Divergences C02 and C03 reconcile.**

1. `*` and `latest` satisfy any version in the schema registry. Retained plans
   must not float, so composition rejects them in manifests and plans.
2. Manifest v1 accepts leading zeros; the schema registry does not. Composition
   uses the registry's `SEMVER_RE`.
3. The pack registry sorts malformed versions low instead of rejecting them.
   Composition only considers versions that parse.
4. v1 coercion drops unknown fields. The v2 manifest rejects unknown fields
   and unknown critical extensions.

**Recommendation, adopted.** The resolver uses the schema registry's
`_semver` and `_satisfies` directly (`src/composition/contracts.py`) and adds
no second range grammar.

## C01.4 Preserved identifiers

`tests/fixtures/composition/preserved_identifiers.json` lists every server
name, alias, tool ID, required-data label and required scope the catalog
emits today, the catalog state values, and the route modules, UI flags and
enrichers of each code-registered pack.

| Kind | Count or values |
| --- | --- |
| MCP servers | 30, including 4 external registrations |
| Tool IDs | 1034 |
| Server aliases | 15 legacy `neuronews-*` aliases |
| Catalog state values | `available`, `degraded`, `empty`, `disabled`, `unauthorized`, `unavailable` |
| Required-data labels | `knowledge-engine-runtime` on 688 tools, `warehouse` on 126, market per-store labels on `noesis-market` tools, and others |
| Routes | Only `news` mounts route modules (`news_routes`, `article_routes`, `sentiment_routes`, `sentiment_trends_routes`, `event_routes`, `event_timeline_routes`, `veracity_routes`) |

Tools attributed to a pack only by hard-coded stem: the three `noesis-research`
tools (`citation_graph`, `literature_claims`, `venues`). The geospatial tools
are used by both the geospatial and science manifests' query examples but are
attributed to no pack.

## C01.5 Journal storage and reconciliation

Decided in [ADR-003](decisions/ADR-003-composition-journal-storage.md):
composition metadata lives in `composition_*` tables on the warehouse
connection; the generation switch is a pointer update written with its
`published` journal stage; startup rebuilds bindings from the active
generation, abandons unfinished activations, and reconciles staged source-owner
operations only from their receipts.

## Per-surface summary

| Surface | Provider code | Authoritative store | Exposure | Version rules |
| --- | --- | --- | --- | --- |
| OSINT | `src/osint/`, `src/analytics/image_reuse.py` | Warehouse documents; no revision store of its own | `noesis-osint` (13 tools) | Source pack `bounded-public-osint` exact versions |
| Research / Science | `src/domains/research/`, `src/kb/research_*`, `src/kb/cultural.py`, `src/kb/mathematics.py` | Documents, research projects, recipes, cultural and math stores | `noesis-research` (3 tools), `noesis-knowledge-engine` | Source packs `primary-scientific-evidence`, `research-discovery`, `openreview-research` |
| Geospatial | `src/kb/geospatial.py`, `src/kb/geospatial_features.py` | Places, geometries, resolutions, spatial receipts, features | `noesis-knowledge-engine` geospatial tools | Source pack `geospatial-berlin`; contracts `noesis-geospatial-*` |
| Sources | `src/ingestion/source_packs.py`, `source_pack_runtime.py`, `source_pack_upgrades.py` | Source-pack versions, runs, cursors, schedules, receipts | `noesis-knowledge-engine` `*_source_pack*` tools | Exact immutable versions, no downgrade |
| Intake | `src/kb/intake_modes.py` and `src/kb/intake_*.py` | Intake sessions and their mode records | `noesis-knowledge-engine` intake tools such as `start_intake_mode` | Session revisions; references carry versions |

## Unknowns

| Unknown | Decision needed | Where it is settled |
| --- | --- | --- |
| OSINT outputs have no revision-addressed store | Whether OSINT references can pin revisions | Plans reference OSINT outputs by document identity; revision addressing is declared `unsupported` in the OSINT provider descriptor (C08) |
| `market` has no code-registered pack | Which unit is the bundle | The `packs/market` manifest is the bundle (C09.2) |
| `energy` declares no capabilities | Whether it is a bundle | It adapts with an empty capability set and contributes only its provisioning template (C09.3) |
| Unreachable implementations listed above | Whether to bind them | Not bound until a caller exists; marked unknown in their manifests (C09.2) |
| Live provider availability | Out of scope for the offline proof | Separate acceptance evidence |
