"""Acceptance-matrix gate (C08.7): every matrix row maps to exactly one named test.

The architecture doc's matrix table must list each row with this test ID, and
each ID must name a test that exists. The tests themselves run with the rest
of ``tests/unit/composition`` in CI; this module guards the mapping.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs/architecture/pack-workflow-composition.md"
FIRST = "tests/unit/composition/test_first_composition.py"

MATRIX = {
    "Two packs consume Geospatial": f"{FIRST}::test_one_geospatial_binding_consumed_by_both_roots",
    "OSINT disabled, Research active": f"{FIRST}::test_disabling_osint_keeps_research_spatial_operations_and_evidence",
    "Required provider missing or ambiguous":
        f"{FIRST}::test_required_provider_missing_or_ambiguous_blocks_before_execution",
    "Optional acquisition unavailable":
        f"{FIRST}::test_optional_acquisition_unavailable_runs_local_analysis_with_missing_source_coverage",
    "Contract/ontology conflict": f"{FIRST}::test_contract_or_ontology_conflict_rejects_the_composition",
    "Upgrade changes a pinned provider/source":
        f"{FIRST}::test_source_revision_preview_blocks_incompatible_and_keeps_historical_pins",
    "Crash during activation": f"{FIRST}::test_crash_during_activation_keeps_the_previous_generation",
    "Crash after a workflow mutation":
        f"{FIRST}::test_crash_after_a_workflow_mutation_reconciles_and_resumes_under_the_original_digest",
    "Access revoked after preflight": f"{FIRST}::test_access_revoked_after_preflight_is_rechecked_everywhere",
    "Several consumers acquire from one account":
        "tests/unit/composition/test_sources.py::"
        "test_aggregate_account_limit_holds_across_consumers_and_is_a_distinct_blocker",
    "Legacy manifest/session/report":
        f"{FIRST}::test_legacy_manifests_keep_identity_and_public_behavior_through_the_adapter",
    "Fixture-only public recipe run":
        "tests/unit/composition/test_workflows.py::"
        "test_fixture_runs_cannot_claim_dispatch_and_dispatch_mode_needs_the_dispatcher",
    # The Economics public-finance feature's offline journey (#2006) composes economics, legal, political,
    # procurement, funding, ownership identity, subscriptions and the source-pack runtime.
    "Public-finance budget line to payments":
        "tests/unit/domains/test_public_finance_acceptance.py::"
        "test_budget_line_to_plans_outturns_payments_findings_acts_dossiers_and_award_context",
    # The Economics demographics feature's offline journey (#2017) composes economics, legal, political,
    # geospatial, subscriptions and the source-pack runtime.
    "Demographic geography to series and definitions":
        "tests/unit/domains/test_demographics_acceptance.py::"
        "test_geography_to_series_definitions_boundaries_and_citations",
    # The Geospatial housing feature's offline journey (#2022) composes geospatial, legal, economics (dataset
    # series), political, news, subscriptions and the source-pack runtime.
    "Address to housing dossier":
        "tests/unit/domains/test_housing_acceptance.py::"
        "test_address_to_housing_dossier_with_conflicts_unknowns_and_restart",
    # The Clinical Evidence surveillance feature's offline journey (#2031) composes clinical, science (claims),
    # geospatial, subscriptions and the source-pack runtime.
    "Condition to surveillance dossier":
        "tests/unit/domains/test_surveillance_acceptance.py::"
        "test_condition_and_geography_to_a_cited_surveillance_dossier",
    # The Clinical Evidence medicines feature's offline journey (#2425) composes clinical records, terms, publication
    # links, subscriptions and the source-pack runtime.
    "Medicine to regulatory timeline":
        "tests/unit/domains/test_medicines_acceptance.py::"
        "test_medicine_to_a_cited_regulatory_timeline_with_label_diffs_and_linked_trials",
    # The Products safety feature's offline journey (#2030) composes products, technology (standards), legal
    # (works), news, entity identity, subscriptions and the source-pack runtime.
    "Product to safety-notice dossier":
        "tests/unit/domains/test_product_safety_acceptance.py::"
        "test_product_gtin_and_brand_model_to_a_cited_notice_dossier",
    # The Funding & Grants development-finance feature's offline journey (#2038) composes funding, economics (CRS
    # through the SDMX connector), geospatial place resolution, ownership and entity identity, research projects,
    # authored reports, subscriptions and the source-pack runtime.
    "Funder to aid activities":
        "tests/unit/domains/test_development_finance_acceptance.py::"
        "test_funder_to_cited_activities_with_coverage_vintages_identity_and_monitoring",
    # The Economics trade features' offline journey (#2555) composes economics (Comtrade, Comext through the Eurostat
    # connector, concordances), geospatial place resolution, legal sanctions correlations, subscriptions and the
    # source-pack runtime.
    # The Products food feature's offline journey (#2297) composes products (food records, identities, safety
    # notices), subscriptions and the source-pack runtime.
    "GTIN to cited food composition":
        "tests/unit/domains/test_food_composition_acceptance.py::"
        "test_gtin_to_cited_composition_label_history_identity_and_linked_notices",
    "Country pair to trade flows":
        "tests/unit/domains/test_trade_flows_acceptance.py::"
        "test_country_pair_and_product_to_cited_flows_with_release_vintages_and_mirror_asymmetries",
    # The Economics labour-statistics feature's offline journey (#2490) composes economics (SDMX and BLS sources in the
    # Economics series storage), geospatial place resolution, subscriptions and the source-pack runtime.
    "Place to labour indicators":
        "tests/unit/domains/test_labour_acceptance.py::"
        "test_place_sector_and_occupation_to_cited_labour_indicators_with_definitions_and_vintages",
    # The On-chain Observations bundle's offline journey (#2058) composes its own record owner, entity identity,
    # the OSINT provenance chain and the source-pack declarations.
    "Contract and address to cited ledger observations":
        "tests/unit/domains/test_onchain_acceptance.py::"
        "test_contract_and_address_to_cited_origin_transfers_and_probable_cluster",
    # The Chemicals and Substances bundle's offline journey (#2316) composes its record owner with Legal works,
    # Products safety notices, entity identity decisions and subscriptions.
    "Substance to cited regulatory dossier":
        "tests/unit/domains/test_chemicals_acceptance.py::"
        "test_substance_name_or_identifier_to_a_cited_regulatory_dossier",
    # The Natural Hazards bundle's offline journey (#2372) composes its own record owner, geospatial places and
    # relations, entity identity, subscriptions and the source-pack runtime.
    "Place and window to cited hazard events":
        "tests/unit/domains/test_natural_hazards_acceptance.py::"
        "test_place_and_window_to_cited_events_with_revision_history_correspondents_and_alerts_as_issued",
    # The Products expansion's offline journey (#2103) composes the appliances and components features with the
    # display bundle, entity identity and the source-pack runtime, with the features off and on.
    "Multi-category product lookup, match and compare":
        "tests/unit/domains/test_products_expansion_acceptance.py::"
        "test_multi_category_lookup_match_compare_and_cite",
    # The Engineering Safety bundle's offline journey (#2077) composes its record owner, Products models and
    # recalls, entity identity, subscriptions and the source-pack declarations.
    "Subject to engineering-safety dossier":
        "tests/unit/engineering_safety/test_acceptance.py::"
        "test_subject_to_cited_engineering_safety_dossier_with_reviews_citations_and_monitoring",
    # The Materials bundle's offline journey (#2091) composes its record owner, the shared identity state machine,
    # Science literature and technology.standards citations and the source-pack runtime.
    "Material to cited property dossier":
        "tests/unit/domains/test_materials_acceptance.py::"
        "test_material_to_cited_property_dossier_comparison_search_citations_and_release_diff",
    # The Astronomy and Space bundle's offline journey (#2161) composes its own record owner, science (papers by
    # bibcode and DOI), entity identity, geospatial (launch sites), subscriptions and the source-pack runtime.
    "Object to cited astronomy history":
        "tests/unit/domains/test_astronomy_acceptance.py::"
        "test_object_to_cited_designations_vintages_dispositions_launches_alerts_and_monitors",
    # The Astronomy space-object registration features' offline journey (#2459) composes the registration record
    # owner with the SATCAT objects, entity and ownership identity, Legal and Science citations, geospatial places
    # and subscriptions.
    "Object to cited registration, operator and re-entry":
        "tests/unit/domains/test_astronomy_registration_acceptance.py::"
        "test_object_to_cited_registration_operator_and_reentry_with_identity_and_revisions",
    # The OSINT movements feature's offline journey (#2289) composes the movement record owner with the
    # source-pack runtime, Fisheries GFW identity, geospatial facilities, ownership identity, sanctions and
    # subscriptions.
    "Aircraft and vessel to cited movements":
        "tests/unit/domains/test_osint_movements_acceptance.py::"
        "test_aircraft_and_vessel_to_cited_registry_records_sampled_movements_and_calls",
    # The Sports bundle's offline journey (#2147) composes its record owner, the source-pack runtime, reviewable
    # identity, the binary forecast ledger and subscriptions.
    "Competition and date to a cited table":
        "tests/unit/domains/test_sports_acceptance.py::"
        "test_competition_and_date_to_a_cited_table_with_revisions_identity_forecast_and_monitor",
    # The Weather bundle's offline journey (#2176) composes its own record owner, Climate & Environment stations
    # and series, Geospatial places and containment, entity identity, subscriptions and the source-pack runtime.
    "Place to cited weather record":
        "tests/unit/domains/test_weather_acceptance.py::"
        "test_place_to_cited_observations_forecasts_warnings_verification_links_and_monitors",
    # The Linguistics pack's offline journey (#2190) composes its record owner, reviewable identity through
    # entity history, the cross-language records, Geospatial places, subscriptions and the source-pack runtime.
    "Word to cited lexeme dossier":
        "tests/unit/domains/test_linguistics_acceptance.py::test_word_to_cited_lexeme_dossier_offline",
    # The OSS Ecosystems bundle's offline journey (#2204) composes its registries, graph and licence providers with
    # technology (identities, inventories, vulnerabilities by citation), entity identity, subscriptions and the
    # source-pack runtime.
    "Package to release history and dependency graphs":
        "tests/unit/domains/test_oss_ecosystems_acceptance.py::"
        "test_package_to_cited_history_graphs_licences_identity_advisories_and_monitoring",
    # The Political legislation features' offline journey (#2451) composes the political dossier store, the
    # source-pack runtime, entity identity, the lobbying links, Legal works and subscriptions.
    "US or UK bill to cited legislative dossier":
        "tests/unit/domains/test_legislation_acceptance.py::"
        "test_us_and_uk_bills_to_cited_dossiers_with_stages_versions_votes_and_linked_disclosures",
    # The Energy Systems bundle's offline journey (#2267) composes its own record owner, climate-environment (the
    # reused ENTSO-E adapter), market price storage, geospatial place resolution, entity identity, subscriptions and
    # the source-pack declarations.
    "Zone or country to cited energy series":
        "tests/unit/domains/test_energy_acceptance.py::"
        "test_zone_and_country_to_cited_energy_series_with_vintages_identity_and_monitoring",
    # The Fisheries and Maritime Activity bundle's offline journey (#2343) composes its record owner with entity
    # identity decisions, subscriptions and, by citation, sanctions and geospatial places.
    "Vessel and area to cited fisheries records":
        "tests/unit/domains/test_fisheries_acceptance.py::"
        "test_vessel_and_area_to_cited_authorisations_listings_effort_and_catch",
    # The Geospatial infrastructure features' offline journey (#2398) compose the infrastructure record owner with
    # the geospatial store and relations, Corporate Ownership identity, Energy Systems links by citation,
    # subscriptions and the source-pack declarations.
    "Place and operator to cited infrastructure assets":
        "tests/unit/domains/test_infrastructure_acceptance.py::"
        "test_place_and_operator_to_cited_assets_with_status_history_identity_and_monitoring",
    # The Humanitarian bundle's offline journey (#2288) composes its own record owner, geospatial places and
    # geometries, entity identity, subscriptions and the source-pack runtime; ACLED is the declined source.
    "Place or crisis to humanitarian dossier":
        "tests/unit/domains/test_humanitarian_acceptance.py::"
        "test_place_and_crisis_to_a_cited_dossier_with_precision_history_identity_and_gaps",
    # The Agriculture and Food Systems bundle's offline journey (#2369) composes its record owner with the Economics
    # series storage, geospatial place resolution, Products safety notices and subscriptions.
    "Commodity and place to cited series":
        "tests/unit/domains/test_agrifood_acceptance.py::"
        "test_commodity_and_place_to_cited_series_with_vintages_and_flags",
    # The Legal courts and justice-statistics features' offline journey (#2430) composes the Legal work model, the
    # source-pack runtime, ownership identity, geospatial places and subscriptions.
    "Provision or party to cited dockets; place to cited justice statistics":
        "tests/unit/domains/test_courts_justice_acceptance.py::"
        "test_provision_and_party_to_cited_dockets_and_place_to_cited_statistics",
    # The Climate and Environment biodiversity feature's offline journey (#2531) composes its record owner with
    # geospatial places and receipts, entity identity decisions, Science papers by DOI and subscriptions.
    "Taxon or place to cited biodiversity records":
        "tests/unit/domains/test_biodiversity_acceptance.py::"
        "test_taxon_and_place_to_cited_occurrences_and_conservation_status_history",
}


@pytest.mark.parametrize(("row", "test_id"), sorted(MATRIX.items()))
def test_every_matrix_row_names_an_existing_test(row, test_id):
    path, name = test_id.split("::")
    module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    assert callable(getattr(module, name, None)), test_id


def test_the_architecture_doc_maps_every_row_to_its_test():
    text = DOC.read_text()
    section = text[text.index("## Acceptance matrix"):text.index("## Effect on the existing expansion backlog")]
    rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| ---")][1:]
    assert {row.split(" | ")[0].lstrip("| ").strip() for row in rows} == set(MATRIX)
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert all(cells), row  # no empty cells
        assert f"`{MATRIX[cells[0]].split('::')[1]}`" in cells[-1], row
