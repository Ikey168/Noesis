"""FC06/FC07 (#2273, #2277): reviewable food identity and citation-only links to Products safety notices."""

from __future__ import annotations

import copy
import json

import pytest

from src.kb.food_composition import FoodCompositionError
from src.kb.food_identity import FoodIdentity
from src.kb.food_notice_links import NO_NOTICE, FoodNoticeLinks
from tests.unit import food_composition_harness as h

NS = h.NS


def seed_product_model(env, gtin: str, brand: str, designation: str) -> str:
    """One Products model with a variant publishing a GTIN (the shape src.kb.products writes)."""
    from src.kb.products import ProductStore

    ProductStore(env.conn)
    model, variant = f"product-model:food-{gtin}", f"product-variant:food-{gtin}"
    for identity, level, parent, identifiers in (
            (model, "model", None, {}),
            (variant, "variant", model, {"gtin": [{"value": gtin, "state": "valid"}]})):
        env.conn.execute(
            "INSERT INTO product_identities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [identity, NS, level, parent, "icecat", brand, designation, None, gtin, "{}", json.dumps(identifiers),
             None, "seed", 1])
    return model


def notice_citing(env, number: str, gtin: str, brand: str, name: str) -> dict:
    """A RASFF-shaped notice statement (from the pinned fixture) citing a GTIN, applied to Products safety."""
    row = env.conn.execute("SELECT statement_json FROM product_safety_revisions WHERE notice_id=? LIMIT 1",
                           [env.notice("rasff", "2026.0457")]).fetchone()
    statement = json.loads(row[0])
    statement.update({"notice_number": number, "title": f"notice {number}"})
    statement["identifications"] = [
        {"kind": "gtin", "value": gtin, "group": 0, "gtin_state": "valid", "locator": {"json_pointer": "/p/gtin"}},
        {"kind": "brand", "value": brand, "group": 0, "locator": {"json_pointer": "/p/brand"}},
        {"kind": "name", "value": name, "group": 0, "locator": {"json_pointer": "/p/name"}},
        {"kind": "batch", "value": "L77", "group": 0, "locator": {"json_pointer": "/p/batch"}},
    ]
    return env.store.apply(NS, statement)


@pytest.fixture()
def env():
    env = h.Env()
    assert env.run_notices()["status"] == "complete"
    assert env.run_food()["status"] == "complete"
    return env


def by_pair(result, left, right):
    return next(c for c in result["candidates"] if {c["left_food_id"], c["right_id"]} == {left, right})


def test_gtin_candidates_conflicts_and_generic_name_proposals(env):
    identity = FoodIdentity(env.conn)
    result = identity.propose(NS, scopes=h.WRITE, principal_id="matcher")
    off_bar = env.food_id("open-food-facts", f"off:{h.BAR}")
    fdc_bar = env.food_id("fooddata-central", "fdc:9990101")
    wafer = env.food_id("fooddata-central", "fdc:9990102")
    same = by_pair(result, off_bar, fdc_bar)
    # 0071000000208 (OFF, EAN-13) and 071000000208 (FDC, UPC-A) are one GTIN; the brands agree.
    assert same["candidate_state"] == "proposed" and same["basis"] == "gtin" and same["review_state"] == "unreviewed"
    conflict = by_pair(result, off_bar, wafer)
    assert conflict["candidate_state"] == "conflict" and "different brands" in conflict["reasons"][0]
    assert {c["match_id"] for c in identity.conflicts(NS, scopes=h.READ)} >= {conflict["match_id"]}
    apple = by_pair(result, env.food_id("fooddata-central", "fdc:9990201"),
                    env.food_id("composition-table", "ciqual:13039"))
    assert apple["basis"] == "same-published-name" and apple["evidence"][0]["shared_tokens"]
    oats = by_pair(result, env.food_id("fooddata-central", "fdc:9990301"),
                   env.food_id("composition-table", "ciqual:9310"))
    assert oats["candidate_state"] == "weak"
    # Nothing is accepted without review, and proposing again changes nothing.
    assert not any(c["accepted"] for c in result["candidates"])
    again = identity.propose(NS, scopes=h.WRITE, principal_id="matcher")
    assert {c["match_id"] for c in again["candidates"]} == {c["match_id"] for c in result["candidates"]}


def test_reviews_are_append_only_and_conflicts_cannot_be_accepted(env):
    identity = FoodIdentity(env.conn)
    result = identity.propose(NS, scopes=h.WRITE, principal_id="matcher")
    off_bar = env.food_id("open-food-facts", f"off:{h.BAR}")
    conflict = by_pair(result, off_bar, env.food_id("fooddata-central", "fdc:9990102"))
    with pytest.raises(FoodCompositionError) as caught:
        identity.review(NS, conflict["match_id"], "accepted", "same code", scopes=h.REVIEW, principal_id="r")
    assert caught.value.code == "conflicting_identifiers"
    same = by_pair(result, off_bar, env.food_id("fooddata-central", "fdc:9990101"))
    identity.review(NS, same["match_id"], "accepted", "GTIN and brand agree", scopes=h.REVIEW, principal_id="r1")
    reviewed = identity.review(NS, same["match_id"], "rejected", "second look", scopes=h.REVIEW, principal_id="r2")
    assert [(r["decision"], r["principal_id"]) for r in reviewed["review_history"]] == [("accepted", "r1"),
                                                                                         ("rejected", "r2")]
    assert all(r["reviewed_at_ms"] for r in reviewed["review_history"]) and not reviewed["accepted"]
    with pytest.raises(FoodCompositionError):
        identity.review(NS, same["match_id"], "accepted", "", scopes=h.REVIEW, principal_id="r")
    resolved = identity.resolve_gtin(NS, "071000000208")
    assert {f["provider"] for f in resolved["foods"]} == {"open-food-facts", "fooddata-central"}
    assert identity.resolve_gtin(NS, "4000000000204")["status"] == "unmatched_gtin"


def test_food_products_match_products_identities_by_gtin(env):
    model = seed_product_model(env, h.KEBAB, "Kebabio", "KB-1000")
    result = FoodIdentity(env.conn).propose(NS, scopes=h.WRITE, principal_id="matcher")
    candidate = next(c for c in result["candidates"] if c["right_kind"] == "product-model")
    assert candidate["right_id"] == model and candidate["candidate_state"] == "proposed"


def test_links_by_brand_and_designation_cite_the_notice_revision(env):
    links = FoodNoticeLinks(env.conn)
    created = links.link(NS, scopes=h.WRITE, principal_id="linker")["linked"]
    kebab = env.food_id("open-food-facts", f"off:{h.KEBAB}")
    rasff = env.notice("rasff", "2026.0457")
    assert [(c["food_id"], c["notice_id"], c["basis"], c["state"]) for c in created] == [
        (kebab, rasff, "brand+designation", "cited")]
    assert links.link(NS, scopes=h.WRITE, principal_id="linker")["linked"] == []  # idempotent
    allergens = [{"relation": "contains", "value": "en:gluten"}]
    answer = links.notices_for(NS, kebab, scopes=h.READ, allergens=allergens)
    notice = answer["notices"][0]
    assert answer["status"] == "notices on record" and notice["notice_number"] == "2026.0457"
    assert notice["link"]["notice_revision_id"] == notice["notice_revision"]["revision_id"]
    assert notice["side_by_side"]["notice_hazards_quoted"][0]["description"] == "Salmonella Enteritidis"
    assert "no causal" in notice["side_by_side"]["note"]
    yoghurt = env.food_id("open-food-facts", f"off:{h.YOGHURT}")
    assert links.notices_for(NS, yoghurt, scopes=h.READ)["status"] == NO_NOTICE


def test_a_later_notice_revision_is_re_evaluated_without_deleting_the_link(env):
    from datetime import date

    links = FoodNoticeLinks(env.conn)
    links.link(NS, scopes=h.WRITE, principal_id="linker")
    rasff = env.notice("rasff", "2026.0457")
    row = env.conn.execute("SELECT statement_json FROM product_safety_revisions WHERE notice_id=?",
                           [rasff]).fetchone()
    later = json.loads(row[0])
    later["revision_date"] = "2026-04-01"
    later["updated"] = "2026-04-01"
    for item in later["identifications"]:
        if item["kind"] == "name":
            item["value"] = "frozen turkey kebab"
    assert env.store.apply(NS, later)["status"] == "revised"
    created = links.link(NS, scopes=h.WRITE, principal_id="linker")["linked"]
    assert [c["state"] for c in created] == ["not_cited_in_revision"]
    kebab = env.food_id("open-food-facts", f"off:{h.KEBAB}")
    assert [r["state"] for r in links.rows(NS, kebab)] == ["cited", "not_cited_in_revision"]
    assert links.notices_for(NS, kebab, scopes=h.READ)["status"] == NO_NOTICE
    assert links.notices_for(NS, kebab, scopes=h.READ, as_of=date(2026, 3, 10))["status"] == "notices on record"


def test_gtin_links_contradictions_and_reviewed_notice_matches(env):
    links = FoodNoticeLinks(env.conn)
    notice_citing(env, "2026.0901", h.YOGHURT, "Otherbrand", "yoghurt")  # same GTIN, another brand
    model = seed_product_model(env, h.KEBAB, "Kebabio", "KB-1000")
    notice_citing(env, "2026.0902", h.KEBAB, "Kebabio", "chicken kebab 1 kg")
    identity = FoodIdentity(env.conn)
    food_match = next(c for c in identity.propose(NS, scopes=h.WRITE, principal_id="m")["candidates"]
                      if c["right_kind"] == "product-model")
    identity.review(NS, food_match["match_id"], "accepted", "GTIN and brand agree", scopes=h.REVIEW,
                    principal_id="reviewer")
    env.store.propose_matches(NS, scopes=h.REVIEW, principal_id="matcher")
    env.accept(env.notice("rasff", "2026.0902"), model)
    created = links.link(NS, scopes=h.WRITE, principal_id="linker")["linked"]
    by_notice = {(c["notice_id"], c["basis"]): c["state"] for c in created}
    assert by_notice[(env.notice("rasff", "2026.0901"), "gtin")] == "contradicted"
    assert by_notice[(env.notice("rasff", "2026.0902"), "gtin")] == "cited"
    assert by_notice[(env.notice("rasff", "2026.0902"), "reviewed-notice-match")] == "cited"
    yoghurt = env.food_id("open-food-facts", f"off:{h.YOGHURT}")
    assert links.notices_for(NS, yoghurt, scopes=h.READ)["status"] == NO_NOTICE
    kebab_rows = links.rows(NS, env.food_id("open-food-facts", f"off:{h.KEBAB}"))
    gtin_row = next(r for r in kebab_rows if r["basis"] == "gtin")
    assert {"kind": "batch", "notice": "L77", "note": "quoted; never used alone"} in gtin_row["evidence"]


def test_linking_never_uses_hazard_or_category_similarity(env):
    links = FoodNoticeLinks(env.conn)
    rasff = env.notice("rasff", "2026.0457")
    row = env.conn.execute("SELECT statement_json FROM product_safety_revisions WHERE notice_id=?",
                           [rasff]).fetchone()
    other = copy.deepcopy(json.loads(row[0]))
    other.update({"notice_number": "2026.0903"})
    other["identifications"] = [{"kind": "name", "value": "yoghurt", "group": 0,
                                 "locator": {"json_pointer": "/p/name"}}]  # a name without a brand
    env.store.apply(NS, other)
    created = links.link(NS, scopes=h.WRITE, principal_id="linker")["linked"]
    assert env.notice("rasff", "2026.0903") not in {c["notice_id"] for c in created}
