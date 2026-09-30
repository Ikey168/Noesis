"""FC08 (#2280): a GTIN's composition and label history as of a date, cited, with explicit unknowns."""

from __future__ import annotations

import pytest

from src.kb.food_composition import FoodCompositionError, FoodCompositionQueries, forbidden_keys
from src.kb.food_identity import FoodIdentity
from src.kb.food_notice_links import NO_NOTICE, FoodNoticeLinks
from tests.unit import food_composition_harness as h

NS = h.NS


@pytest.fixture()
def env():
    env = h.Env()
    assert env.run_notices()["status"] == "complete"
    assert env.run_food()["status"] == "complete"
    FoodIdentity(env.conn).propose(NS, scopes=h.WRITE, principal_id="matcher")
    FoodNoticeLinks(env.conn).link(NS, scopes=h.WRITE, principal_id="linker")
    return env


def test_revision_current_at_the_date_per_provider_and_the_chain_on_request(env):
    queries = FoodCompositionQueries(env.conn)
    earlier = queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, as_of="2026-01-15")
    (entry,) = earlier["sources"]
    assert entry["citation"]["revision"]["value"] == "4" and entry["later_revisions"][0]["revision"] == "7"
    assert {a["value"] for a in entry["label"]["allergen_declarations"]} == {"en:gluten"}
    current = queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, include_history=True)
    (entry,) = current["sources"]
    assert entry["citation"]["revision"]["value"] == "7"
    assert [r["revision"] for r in entry["revision_history"]] == ["4", "7"]
    assert [r["current_as_of"] for r in entry["revision_history"]] == [False, True]
    history = queries.label_history(NS, scopes=h.READ, gtin=h.KEBAB)
    assert [r["revision"] for r in history["histories"][0]["revisions"]] == ["4", "7"]


def test_every_value_is_cited_with_odbl_attribution_where_it_applies(env):
    answer = FoodCompositionQueries(env.conn).answer(NS, scopes=h.READ, gtin="071000000208")
    providers = {e["provider"]: e for e in answer["sources"]}
    # The UPC-A in FDC and the EAN-13 in OFF are one GTIN; the other brand's FDC record shares it too.
    assert set(providers) == {"open-food-facts", "fooddata-central"}
    assert answer["provenance_classes"] == ["crowd-sourced", "reference"]
    for entry in answer["sources"]:
        for values in entry["label"].values():
            for value in values:
                cite = value["cite"]
                assert cite["provider"] and cite["provider_key"] and cite["revision_id"] and cite["retrieved_at"]
                assert ("attribution" in cite) == (entry["provider"] == "open-food-facts")
    assert any(a["licence"] == "ODbL-1.0" for a in answer["attributions"])
    assert answer["identity"]["conflicts"]  # the same GTIN under another brand is shown, not resolved


def test_crowd_sourced_and_reference_values_are_shown_side_by_side_never_reconciled(env):
    answer = FoodCompositionQueries(env.conn).answer(NS, scopes=h.READ, gtin=h.BAR)
    rows = {r["nutrient"]: r for r in answer["side_by_side_nutrients"]}
    assert rows["protein"]["comparison"] == "differs as published"
    assert rows["protein"]["classes"] == ["crowd-sourced", "reference"]
    assert {v["amount"] for v in rows["protein"]["values"]} == {"8.0", "7.5"}
    # OFF sodium in g and FDC sodium in mg are never converted.
    assert rows["sodium"]["comparison"].startswith("not comparable")


def test_unknowns_are_explicit(env):
    queries = FoodCompositionQueries(env.conn)
    unmatched = queries.answer(NS, scopes=h.READ, gtin=h.UNKNOWN)
    assert unmatched["status"] == "unmatched_gtin" and unmatched["sources"] == []
    assert unmatched["unknowns"][0]["kind"] == "unmatched_gtin"
    before = queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, as_of="2025-06-01")
    assert before["sources"][0]["status"] == "no label history before this date"
    assert {u["kind"] for u in before["unknowns"]} >= {"no_label_history_before_date"}
    bar = queries.answer(NS, scopes=h.READ, gtin=h.BAR)
    kinds = {(u["kind"], u.get("provider")) for u in bar["unknowns"]}
    assert ("unit_absent", "fooddata-central") in kinds  # label nutrients publish no unit
    oats = queries.answer(NS, scopes=h.READ, food="fooddata-central:fdc:9990301")
    assert {"kind": "unit_unknown", "provider": "fooddata-central", "nutrient": "999", "unit_published": "XYZ"} \
        in oats["unknowns"]
    earlier = queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, as_of="2026-01-15")["unknowns"]
    assert {"kind": "nutrient_not_published", "nutrient": "sugars", "provider": "open-food-facts",
            "provider_key": f"off:{h.KEBAB}"} in earlier
    with pytest.raises(FoodCompositionError):
        queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, as_of="15/01/2026")


def test_linked_notices_and_no_notice_on_record(env):
    queries = FoodCompositionQueries(env.conn)
    kebab = queries.answer(NS, scopes=h.READ, gtin=h.KEBAB)
    assert kebab["notices"]["status"] == "notices on record"
    notice = kebab["notices"]["notices"][0]
    assert notice["notice_number"] == "2026.0457" and notice["link"]["basis"] == "brand+designation"
    quoted = notice["side_by_side"]["label_allergens_quoted"]
    assert {"relation": "contains", "value": "en:soybeans"} in quoted
    # Before the notice was published there was no notice on record.
    assert queries.answer(NS, scopes=h.READ, gtin=h.KEBAB, as_of="2026-02-01")["notices"]["status"] == NO_NOTICE
    yoghurt = queries.answer(NS, scopes=h.READ, gtin=h.YOGHURT)
    assert yoghurt["notices"]["status"] == NO_NOTICE and "not a statement" in yoghurt["notices"]["note"]


def test_generic_foods_with_accepted_matches_and_no_scores(env):
    identity = FoodIdentity(env.conn)
    apple = env.food_id("fooddata-central", "fdc:9990201")
    match = next(m for m in identity.matches_for(NS, apple) if m["basis"] == "same-published-name")
    identity.review(NS, match["match_id"], "accepted", "same food by name", scopes=h.REVIEW, principal_id="r")
    answer = FoodCompositionQueries(env.conn).answer(NS, scopes=h.READ, food="fooddata-central:fdc:9990201")
    assert {e["provider"] for e in answer["sources"]} == {"fooddata-central", "composition-table"}
    assert {e["connected_by"]["kind"] for e in answer["sources"]} == {"requested", "accepted-match"}
    protein = next(r for r in answer["side_by_side_nutrients"] if r["nutrient"] == "protein")
    assert protein["comparison"] == "differs as published"
    assert not forbidden_keys(answer)
    body = {k: v for k, v in answer.items() if k != "boundary"}
    assert not any(k in str(body).lower() for k in ("nutriscore", "nutri-score", "health rating", "diet advice"))
