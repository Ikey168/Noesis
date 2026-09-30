"""FC03-FC05 (#2257, #2264, #2269): Open Food Facts, FoodData Central and Ciqual acquisition, offline."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.food_composition_sources import (
    LIVE_VERIFICATION,
    TABLE_DECISIONS,
    FoodCompositionAdapter,
    fixture_transport,
    selection_entries,
)
from src.ingestion.source_packs import SourcePackError
from tests.unit import food_composition_harness as h

NS = h.NS


@pytest.fixture()
def env():
    env = h.Env()
    assert env.run_food()["status"] == "complete"
    return env


def test_open_food_facts_revisions_flags_exclusions_and_odbl(env):
    food_id = env.food_id("open-food-facts", f"off:{h.KEBAB}")
    revisions = env.food.revisions(NS, food_id)
    assert [r["revision_value"] for r in revisions] == ["4", "7"]
    current = env.food.current_revision(NS, food_id)
    statement = env.food.statement(NS, current["revision_id"])
    assert statement["provenance_class"] == "crowd-sourced"
    assert statement["attribution"]["licence"] == "ODbL-1.0" and statement["attribution"]["share_alike"]
    assert statement["source_fields"]["data_quality_tags"] == ["en:nutrition-data-incomplete"]
    assert statement["source_fields"]["completeness"] == 0.7
    parts = env.food.parts(NS, current["revision_id"])
    assert {(a["relation"], a["value"]) for a in parts["allergen_declarations"]} == {
        ("contains", "en:gluten"), ("contains", "en:soybeans"), ("may_contain", "en:milk")}
    assert {i["language"] for i in parts["ingredient_statements"]} == {"en", "de"}
    ids = {n["nutrient_id"] for n in parts["nutrient_values"]}
    # OFF scores and OFF estimates are never stored.
    assert "nutrition-score-fr" not in ids and not any("estimate" in i for i in ids)
    assert not any(word in str(statement) for word in ("nutriscore", "nova_group", "ecoscore", "image_url"))
    receipts = env.conn.execute(
        "SELECT selection_index, outcome, dropped_json FROM food_selection WHERE source_id='off-food-products' "
        "ORDER BY selection_index").fetchall()
    assert [r[1] for r in receipts] == ["returned", "returned", "returned", "returned", "not_found"]
    assert "nutriscore_grade" in receipts[0][2] and "image_url" in receipts[0][2]
    assert "nutriments/nutrition-score-fr_100g" in receipts[0][2]


def test_fdc_nutrients_units_derivation_and_label_nutrients(env):
    bar = env.food.current_revision(NS, env.food_id("fooddata-central", "fdc:9990101"))
    statement = env.food.statement(NS, bar["revision_id"])
    assert statement["provenance_class"] == "reference" and statement["identifiers"]["data_type"] == "Branded"
    assert statement["identifiers"]["gtin"]["value"] == "071000000208"
    assert bar["revision_value"] == "11/20/2025" and bar["revision_date"] == "2025-11-20"
    nutrients = env.food.parts(NS, bar["revision_id"])["nutrient_values"]
    sodium = next(n for n in nutrients if n["nutrient_id"] == "307")
    assert sodium["unit"] == {"published": "MG", "normalized": "mg", "state": "known"}
    assert sodium["derivation"]["code"] == "LCCS" and sodium["value_kind"] == "food-nutrient"
    label = [n for n in nutrients if n["value_kind"] == "label-nutrient"]
    assert label and all(n["unit"]["state"] == "absent" and n["basis"] == "per serving (40 g)" for n in label)
    oats = env.food.current_revision(NS, env.food_id("fooddata-central", "fdc:9990301"))
    unknown = next(n for n in env.food.parts(NS, oats["revision_id"])["nutrient_values"] if n["nutrient_id"] == "999")
    assert unknown["unit"] == {"published": "XYZ", "normalized": None, "state": "unknown"}


def test_newer_fdc_publication_is_a_new_revision(env):
    native = h.pages("fdc-foods")
    native[2]["body"]["publicationDate"] = "10/1/2025"
    native[2]["body"]["foodNutrients"][1]["amount"] = 0.2
    adapters = {"fdc-foods": env.compiled_food("fdc-foods", native)}
    assert env.run_food("food-2", adapters=adapters, source_ids=["fdc-foods"])["status"] == "complete"
    apple = env.food_id("fooddata-central", "fdc:9990201")
    assert [r["revision_value"] for r in env.food.revisions(NS, apple)] == ["4/1/2025", "10/1/2025"]
    # Unchanged foods on the same run add nothing.
    assert len(env.food.revisions(NS, env.food_id("fooddata-central", "fdc:9990101"))) == 1


def test_ciqual_edition_values_flags_and_tagnames(env):
    apple = env.food.current_revision(NS, env.food_id("composition-table", "ciqual:13039"))
    assert apple["revision_basis"] == "table-edition" and apple["revision_value"] == "2020"
    nutrients = {n["nutrient_id"]: n for n in env.food.parts(NS, apple["revision_id"])["nutrient_values"]}
    assert nutrients["25000"]["tagname"] == "PROCNT"
    assert nutrients["25000"]["amount"] == "0,25" and nutrients["25000"]["amount_decimal"] == "0.25"
    assert nutrients["40000"]["value_flags"]["value_type"] == "trace" and nutrients["40000"]["amount_decimal"] is None
    assert nutrients["10110"]["value_flags"]["value_type"] == "less-than"
    assert nutrients["10110"]["unit"]["normalized"] == "mg" and nutrients["328"]["unit"]["normalized"] == "kcal"
    oats = env.food.current_revision(NS, env.food_id("composition-table", "ciqual:9310"))
    sugars = next(n for n in env.food.parts(NS, oats["revision_id"])["nutrient_values"] if n["nutrient_id"] == "32000")
    assert sugars["value_flags"]["value_type"] == "missing"
    # Unselected and absent codes are not records.
    assert {i["provider_key"] for i in env.food.items(NS, provider="composition-table")} == {"ciqual:13039",
                                                                                              "ciqual:9310"}


def test_a_new_table_edition_keeps_the_prior_edition(env):
    native = copy.deepcopy(h.pages("ciqual-composition"))
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    src = h.safety.source("ciqual-composition", installed)
    entry = {**src["food_composition"]["selection"][0], "edition": "2025", "edition_date": "2025-11-05"}
    src["food_composition"]["selection"] = [entry]
    adapter = FoodCompositionAdapter(src, transport=fixture_transport(native))
    page = adapter.fetch_page({"operation": "food", "parameters": {}, "limit": 50}, cursor=None)
    for record in page.records:
        env.food.apply(NS, record["food_composition"])
    apple = env.food_id("composition-table", "ciqual:13039")
    assert [r["revision_value"] for r in env.food.revisions(NS, apple)] == ["2020", "2025"]


def test_declined_tables_and_bad_selectors_are_refused():
    src = h.source("ciqual-composition")
    assert TABLE_DECISIONS["efsa"]["decision"] == "reference-only" and TABLE_DECISIONS["ciqual"]["decision"] == "acquire"
    for table in ("efsa", "bls", "cofid", "frida"):
        bad = copy.deepcopy(src)
        bad["food_composition"]["selection"][0]["table"] = table
        with pytest.raises(SourcePackError) as caught:
            selection_entries(bad)
        assert caught.value.code == "access_decision"
        assert LIVE_VERIFICATION[f"composition-table:{table}"]["status"] == "not-implemented"
    off = h.source("off-food-products")
    off["food_composition"]["selection"] = [{"gtin": "4000000000106"}]  # bad check digit
    with pytest.raises(SourcePackError):
        selection_entries(off)
    off["endpoint"] = "https://example.org"
    with pytest.raises(SourcePackError):
        selection_entries(off)


def test_fdc_needs_its_key_and_rate_limits_are_classified():
    src = h.source("fdc-foods")
    with pytest.raises(SourcePackError) as caught:
        FoodCompositionAdapter(src, transport=fixture_transport(h.pages("fdc-foods"))).fetch_page(
            {"operation": "food", "parameters": {}}, cursor=None)
    assert caught.value.code == "authentication_failed"
    limited = h.pages("fdc-foods")
    limited[0].update({"status": 429, "headers": {"Retry-After": "60"}})
    with pytest.raises(SourcePackError) as caught:
        FoodCompositionAdapter(src, transport=fixture_transport(limited), secret="k").fetch_page(
            {"operation": "food", "parameters": {}}, cursor=None)
    assert caught.value.code == "rate_limited"


def test_every_provider_is_unverified_live_with_an_intended_status():
    assert {k: v["status"] for k, v in LIVE_VERIFICATION.items() if not k.startswith("composition-table:")
            or k.endswith("ciqual")} == {"open-food-facts": "unverified-live", "fooddata-central": "unverified-live",
                                         "composition-table:ciqual": "unverified-live"}
    assert all("intended" in v for v in LIVE_VERIFICATION.values())
