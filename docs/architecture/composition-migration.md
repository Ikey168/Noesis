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
| geospatial | `geospatial.core`, `geospatial.transit`; optional `housing` feature (default off) adds `geospatial.housing` and binds `legal.core`, `economics.core`, `political.core`, `news.core`, `platform.subscriptions` and `platform.source-runtime`; optional `housing-transit-context` feature (default off) adds the transit stops of `geospatial.transit` as accessibility context | v1 manifest unchanged; pins `geospatial-berlin` ^1.1.0, and the `housing` sources ship as the `geospatial-berlin` 1.3.0 upgrade (`packs/geospatial/source_packs/geospatial-berlin-1.3.0.json`) | `test_first_composition.py`, `tests/unit/composition/test_geospatial_housing_composition.py` |
| legal | `legal.core`; optional `sanctions` feature (default off) adds `legal.sanctions` and binds `ownership.core`, `market.lei`, `platform.entity-identity`, `economics.core` and `platform.subscriptions`; optional `federal-statutes` feature (default off, independent of `sanctions`) adds `legal.federal-statutes` and binds `political.core` (Bundestag DIP dossiers), `platform.subscriptions` and `platform.source-runtime` | v1 manifest (`packs/legal/pack.json`) unchanged; ships `legal-research` 1.3.0 (1.1.0 added the sanctions sources; 1.2.0 adds `cellar-product-safety-acts-eng`; 1.3.0 adds the `gii-federal-statutes`, `ris-federal-statute-versions` and `bgbl-federal-promulgations` sources and changes no existing source, so every `^1.1.0`/`^1.0.0` pin still resolves; `legal.federal-statutes` pins `^1.3.0`) | shadow report; `tests/unit/composition/test_legal_sanctions_composition.py`, `tests/unit/composition/test_legal_federal_statutes_composition.py` |
| market | `market.core`, `market.lei`; optional `bafin-notices` feature (default off) adds `market.bafin` (BaFin voting-rights notifications, managers' transactions, net short positions, company database, warnings and measures under the Market publication cutoffs) and binds `platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime` | v1 manifest (`packs/market/pack.json`) plus code pack merged, unchanged; ships the new `bafin-capital-market-notices` 1.0.0 source pack (`market.bafin` pins `^1.0.0`), so the existing `economic-statistics-and-filings` `^1.1.0` pin is untouched | shadow report; `tests/unit/composition/test_market_bafin_composition.py` |
| economics (legacy `economic`) | `economics.core`; optional `public-finance` feature (default off) adds `economics.public-finance` and binds `legal.core`, `political.core`, `procurement.core`, `funding.core`, `ownership.core`, `platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime`; optional `demographics` feature (default off, independent of `public-finance`) adds `economics.demographics` and binds `legal.core`, `political.core`, `geospatial.core`, `platform.subscriptions` and `platform.source-runtime` | alias `economic`; ships `economic-statistics-and-filings` 1.3.0 | shadow report; `tests/unit/composition/test_economics_public_finance_composition.py`, `tests/unit/composition/test_economics_demographics_composition.py` |
| political | `political.core`; optional `lobbying` feature (default off) adds `political.lobbying` and binds `ownership.core`, `market.lei`, `platform.entity-identity` and `platform.subscriptions`; optional `elections` feature (default off, independent of `lobbying`) adds `political.elections` and binds `geospatial.core`, `news.core`, `ownership.core`, `platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime` | v1 manifest plus code pack merged; ships `official-political-records` 1.2.0 | shadow report; `tests/unit/composition/test_political_lobbying_composition.py`, `tests/unit/composition/test_political_elections_composition.py` |
| technology (legacy `technical`) | `technology.core`, `technology.patents`, `technology.standards`; optional `vulnerabilities` feature (default off) adds `technology.vulnerabilities` and binds `products.core`, `platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime` | alias `technical`; ships `technical-software-knowledge` 1.2.0 | shadow report; `tests/unit/composition/test_technology_vulnerabilities_composition.py` |
| products | `products.core`; optional `safety` feature (default off) adds `products.safety` and binds `technology.standards`, `legal.core`, `news.core`, `platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime` | v1 manifest unchanged; ships `products-displays` 1.1.0 (1.0.0 sources verbatim plus the Safety Gate, CPSC, NHTSA and RASFF notice sources; `products.core` keeps its `^1.0.0` range) and consumes `legal-research` 1.2.0 for the cited acts | shadow report; `tests/unit/composition/test_products_safety_composition.py` |
| funding-grants | `funding.core` plus the shared `platform.*` providers; optional `development-finance` feature (default off) adds `funding.development-finance` (IATI activities per publisher, OECD CRS vintages, World Bank projects, reviewable organisation identity and monitors) and binds `economics.core` (economic knowledge), `geospatial.core` (place resolution), `ownership.core` (identity candidates), `platform.entity-identity` and `platform.source-runtime` | authored natively (`packs/funding-grants/manifest.json`); alias `funding`; the CRS source ships in the additive `economic-statistics-and-filings` 1.4.0 source pack (existing sources verbatim), which `funding.development-finance` pins as ^1.4.0 while the Economics pins (^1.3.0, ^1.1.0) still resolve | `test_funding_manifest_resolves_to_its_own_provider_plus_shared_providers`, `test_disabling_funding_is_a_selection_change_that_keeps_shared_providers`, `tests/unit/composition/test_funding_development_finance_composition.py` |
| corporate-ownership | `ownership.core` plus `market.lei`, `platform.entity-identity`, `platform.source-runtime` and `platform.authored-reports`; optional `bafin-voting-rights` feature (default off) binds `market.bafin` and projects voting-rights notifications as `voting_rights` control assertions (the composition runs from Corporate Ownership to Market because Corporate Ownership already depends on `market.lei`; a Market feature requiring ownership would be a bundle cycle) | authored natively (`packs/corporate-ownership/manifest.json`); alias `ownership` | `test_ownership_manifest_resolves_to_its_own_provider_plus_shared_providers`, `test_disabling_ownership_is_a_selection_change_that_keeps_shared_providers` |
| procurement | `procurement.core`; binds `funding.core` (reused capabilities, re-exported so neither bundle is the other's dependency root), `market.lei` and shared `platform.*` providers | authored natively (`packs/procurement/manifest.json`); alias `public-procurement` | `tests/unit/composition/test_procurement_composition.py` |
| clinical-evidence | `clinical.core` plus `science.literature`, `science.methodology`, `science.systematic-reviews`, `science.paper-families`, `platform.source-runtime` and `platform.subscriptions`; optional `surveillance` feature (default off) adds `clinical.surveillance` (public-health surveillance series, vintages and monitors) and binds `geospatial.core` (feature query and place resolution) and `science.literature` (claims beside a series) | authored natively (`packs/clinical-evidence/manifest.json`); alias `clinical`; the bundle keeps its `clinical-evidence` ^0.1.0 pin, and the surveillance sources ship in the additive `clinical-evidence` 0.1.1 source pack (existing sources verbatim), which `clinical.surveillance` pins as ^0.1.1 | `tests/unit/composition/test_clinical_composition.py`, `tests/unit/composition/test_clinical_surveillance_composition.py` |
| climate-environment | `environment.core` plus the shared `geospatial.core`, `geospatial.transit`, `market.lei`, `platform.source-runtime` and `platform.subscriptions` providers | authored natively (`packs/climate-environment/manifest.json`); alias `environment`; ships the `climate-environment` source pack and the `geospatial-berlin` 1.2.0 upgrade (Umweltatlas) | `test_climate_environment_manifest_resolves_to_its_own_provider_plus_shared_providers`, `test_disabling_climate_environment_is_a_selection_change_that_keeps_geospatial` |
| onchain | `onchain.core` (served by its own read-only `noesis-onchain` server) plus the shared `platform.entity-identity` provider; optional `acquisition` feature (default off) binds `platform.source-runtime`; consumed by the OSINT `onchain` optional feature (default off) for organization entities only | authored natively (`packs/onchain/manifest.json`, with `packs/onchain/composition.json` as its checked composition view); alias `onchain-observations`; ships the `onchain-observations` 1.0.0 source pack (Etherscan V2, Blockstream Esplora, ethereum-lists tokens) with no runtime projector: acquisition is the receipted API in `src/ingestion/onchain.py` | `tests/unit/onchain/test_onchain_composition.py` |
| sports | `sports.football` (records, tables as of a date, identity, news links, monitors) and `sports.olympics`, binding `platform.source-runtime`, `platform.entity-identity`, `platform.subscriptions`, `geospatial.core` and `news.core`; optional `sports-identity` feature (default off) binds `ownership.core` for cross-source identity review, so Corporate Ownership never becomes mandatory; optional `sports-tennis` feature (default off, licence-gated CC BY-NC-SA 4.0) adds `sports.tennis`; optional `sports-forecasts` feature (default off) adds `sports.forecasts` over the existing binary forecast ledger | new v1 manifest (`packs/sports/pack.json`) plus `packs/sports/composition.json`; ships the `sports-records` 1.0.0 source pack | `tests/unit/sports/test_sports_composition.py` |

The `energy` example pack was retired on 2026-09-26: it declared no capability
that any provider implemented (a keyword enricher, a panel and a provisioning
template only), so `packs/energy/pack.json` was removed rather than composed.
`test_every_bundle_adapts_and_round_trips_to_its_v1_registration` asserts it no
longer adapts. Energy coverage belongs to the Climate and Environment bundle tracked in
[#1849](https://github.com/Ikey168/Noesis/issues/1849).

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
| `noesis-housing-record-v1` | `src.kb.housing` | `geospatial.housing` | `geospatial-berlin` (1.3.0) |
| `noesis-surveillance-record-v1` | `src.kb.surveillance` | `clinical.surveillance` | `clinical-evidence` (0.1.1) |
| `noesis-product-record-v1` | `src.kb.products` | `products.core` | `products-displays` |
| `noesis-product-safety-notice-v1` | `src.kb.product_safety` | `products.safety` | `products-displays` |
| `noesis-legal-record-v1` | `src.kb.legal` | `legal.core` | `legal-research` |
| `noesis-cultural-object-v1` | `src.kb.cultural` | `science.cultural` | `primary-scientific-evidence` |
| `noesis-patent-part-v1` | `src.kb.patents` | `technology.patents` | `research-discovery` |
| `noesis-lei-part-v1` | `src.kb.lei` | `market.lei` | `economic-statistics-and-filings` |
| `noesis-standard-catalogue-v1` | `src.kb.standards` | `technology.standards` | `technical-software-knowledge` |
| `noesis-math-record-v1` | `src.kb.mathematics` | `science.mathematics` | `research-discovery` |
| `noesis-ownership-part-v1` | `src.kb.ownership_store` | `ownership.core` | `corporate-ownership` |
| `noesis-procurement-record-v1` | `src.kb.procurement_notices` | `procurement.core` | `procurement` |
| `noesis-sanctions-record-v1` | `src.kb.sanctions` | `legal.sanctions` | `legal-research` |
| `noesis-vulnerability-record-v1` | `src.kb.vulnerabilities` | `technology.vulnerabilities` | `technical-software-knowledge` |
| `noesis-lobbying-record-v1` | `src.kb.lobbying` | `political.lobbying` | `official-political-records` |
| `noesis-election-record-v1` | `src.kb.elections` | `political.elections` | `official-political-records` |
| `noesis-public-finance-record-v1` | `src.kb.public_finance` | `economics.public-finance` | `economic-statistics-and-filings` |
| `noesis-demographic-series-v1` | `src.kb.demographics` | `economics.demographics` | `economic-statistics-and-filings` |
| `noesis-development-finance-record-v1` | `src.kb.development_finance` | `funding.development-finance` | `economic-statistics-and-filings` (1.4.0) |
| `noesis-bafin-notice-v1` | `src.domains.market.bafin_notices` | `market.bafin` | `bafin-capital-market-notices` |
| `noesis-sports-record-v1` | `src.kb.sports_store` | `sports.football` | `sports-records` |

Music remains a possible future bundle and is neither migrated nor scaffolded.

## Legacy paths intentionally kept

| Path | Why it stays | Owner |
| --- | --- | --- |
| `src/domains/registry.py` `enable_pack`/`disable_pack`/`load_config` | Delegating shims. They still serve bundles that are not cut over in a deployment (no composition tables, or a compatibility rollback). They never write enablement for a composition-managed bundle. | lifecycle coordinator |
| `src/domains/pack_install.py` `install_manifest`/`uninstall` | The legacy installer for non-composed manifests; it refuses composition-managed names. | lifecycle coordinator |
| `src/mcp_host/catalog.py` `_server_pack` / `_required_data` tables | The generated catalog artifact (`contracts/generated/noesis-mcp-catalog-v1.json`) is built without a deployment's active plan, and unbound tools have no descriptor. The tables are the fallback, never an authority. | MCP catalog |
| `src/kb/funding_bundle.py` per-namespace flag | The authority only until Funding & Grants is cut over. Afterwards `set_enabled` routes to the coordinator and the flag is never written. | lifecycle coordinator |
| `src/kb/environment_bundle.py` per-namespace flag | Same rule for Climate and Environment: the authority only until the bundle is cut over; afterwards `set_enabled` is a coordinator selection change. | lifecycle coordinator |
