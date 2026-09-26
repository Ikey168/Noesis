# Composition migration record

Status: all bundles and source-pack projectors migrated on 2026-09-26 (C09,
[#1797](https://github.com/Ikey168/Noesis/issues/1797)). The procedure is the
one proven for OSINT + Research + Geospatial in C08. For each bundle:
1. Adapt it through the read-only adapter plus a `packs/<bundle>/composition.json` overlay.
2. Register provider descriptors for what it actually implements.
3. Commit an annotated shadow diff.
4. Cut lifecycle ownership over to the coordinator.

The work landed on one branch rather than one PR per bundle. The ownership
statements and acceptance evidence below are the per-bundle record.

## Ownership statements

After cutover, the composition lifecycle coordinator
(`src/composition/lifecycle.py`) is the only enabled-state authority for every
bundle below. Legacy `enable_pack`/`disable_pack` and `install`/`uninstall`
calls either delegate (a no-op when the state already matches) or raise a
compatibility error. `rollback_to_legacy` is the compatibility flag. The
acceptance evidence for every bundle is
`tests/unit/composition/test_migration.py::test_every_bundle_migrates_with_one_authority_and_unchanged_source_pins`,
`::test_no_retired_legacy_path_changes_enabled_state_independently_of_the_coordinator`
and `::test_shadow_diff_is_annotated_for_every_migrated_bundle`.

| Bundle | Providers it contributes | Retained compatibility | Further evidence |
| --- | --- | --- | --- |
| news | `news.core` | code pack `news`; route modules kept as advisory registrations | shadow report |
| science (legacy `research`) | `science.literature`, `science.mathematics`, `science.cultural` (optional `cultural-collections` feature) | alias `research`; code enrichers kept | `test_first_composition.py` |
| osint | `osint.core` (imagery provenance behind `osint-review`) | v1 `packs/osint/pack.json` unchanged | `test_first_composition.py` |
| geospatial | `geospatial.core`, `geospatial.transit` | v1 manifest and Berlin source pack unchanged | `test_first_composition.py` |
| legal | `legal.core` | v1 manifest plus code pack merged | shadow report |
| market | `market.core`, `market.lei` | v1 manifest plus code pack merged | shadow report |
| economics (legacy `economic`) | `economics.core` | alias `economic` | shadow report |
| political | `political.core` | v1 manifest plus code pack merged | shadow report |
| technology (legacy `technical`) | `technology.core`, `technology.patents`, `technology.standards` | alias `technical` | shadow report |
| products | `products.core` | v1 manifest unchanged | shadow report |
| energy | none: its v1 capabilities have no implementation and stay declared but unbound | v1 manifest unchanged | `test_capabilities_without_an_implementation_stay_unbound` |
| funding-grants | `funding.core` plus the shared `platform.*` providers | authored natively (`packs/funding-grants/manifest.json`); alias `funding` | `test_funding_manifest_resolves_to_its_own_provider_plus_shared_providers`, `test_disabling_funding_is_a_selection_change_that_keeps_shared_providers` |

Legacy v1 capability names stay declared with `legacy.<bundle>.<name>`
contracts and are never bound. Binding happens only through capabilities a
provider descriptor implements.

## Projector record owners

Each source-pack projector has one named record owner and one provider
descriptor, which declares the source pack as the provider's source. No
projector introduces a store. Evidence:
`test_every_projector_has_one_record_owner_one_descriptor_and_its_source_pack`.

| Projector schema | Record owner | Provider | Source pack |
| --- | --- | --- | --- |
| `noesis-geospatial-feature-v1` | `src.kb.geospatial_features` | `geospatial.core` | `geospatial-berlin` |
| `noesis-transit-feed-v1` | `src.kb.transit` | `geospatial.transit` | `geospatial-berlin` |
| `noesis-product-record-v1` | `src.kb.products` | `products.core` | `products-displays` |
| `noesis-legal-record-v1` | `src.kb.legal` | `legal.core` | `legal-research` |
| `noesis-cultural-object-v1` | `src.kb.cultural` | `science.cultural` | `primary-scientific-evidence` |
| `noesis-patent-part-v1` | `src.kb.patents` | `technology.patents` | `research-discovery` |
| `noesis-lei-part-v1` | `src.kb.lei` | `market.lei` | `economic-statistics-and-filings` |
| `noesis-standard-catalogue-v1` | `src.kb.standards` | `technology.standards` | `technical-software-knowledge` |
| `noesis-math-record-v1` | `src.kb.mathematics` | `science.mathematics` | `research-discovery` |

Music remains a possible future bundle and is neither migrated nor scaffolded.

## Legacy paths intentionally kept

| Path | Why it stays | Owner |
| --- | --- | --- |
| `src/domains/registry.py` `enable_pack`/`disable_pack`/`load_config` | Delegating shims. They still serve bundles that are not cut over in a deployment (no composition tables, or a compatibility rollback). They never write enablement for a composition-managed bundle. | lifecycle coordinator |
| `src/domains/pack_install.py` `install_manifest`/`uninstall` | The legacy installer for non-composed manifests; it refuses composition-managed names. | lifecycle coordinator |
| `src/mcp_host/catalog.py` `_server_pack` / `_required_data` tables | The generated catalog artifact (`contracts/generated/noesis-mcp-catalog-v1.json`) is built without a deployment's active plan, and unbound tools have no descriptor. The tables are the fallback, never an authority. | MCP catalog |
| `src/kb/funding_bundle.py` per-namespace flag | The authority only until Funding & Grants is cut over. Afterwards `set_enabled` routes to the coordinator and the flag is never written. | lifecycle coordinator |
