"""Products safety notices: product-to-notices answers as of a date with cited revisions (R08, #2011)."""

from __future__ import annotations

import pytest

from src.kb.product_safety import NO_NOTICE, ProductSafetyError
from tests.unit import product_safety_harness as h

NS = h.NS


@pytest.fixture(scope="module")
def env():
    item = h.Env().loaded()
    item.accept(
        item.notice("safety-gate", "SR/00417/26"), item.model("icecat", "EX-32U8")
    )
    item.accept(
        item.notice("safety-gate", "SR/00431/26"), item.model("icecat", "EX-27Q4")
    )
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00417/26")
    page["body"]["alert"]["lastUpdateDate"] = "2026-04-02"
    page["body"]["alert"]["measures"].append(
        {
            "measureType": "Stop of sales",
            "takenBy": "Authorities",
            "category": "Compulsory measures",
        }
    )
    page["body"]["alert"]["followUps"] = [
        {
            "date": "2026-04-02",
            "country": {"code": "FR", "name": "France"},
            "text": "Found on the French market.",
            "measures": [{"measureType": "Withdrawal of the product from the market"}],
        }
    ]
    item.run_notices(
        "update",
        adapters={"safety-gate-alerts": item.compiled("safety-gate-alerts", native)},
        source_ids=["safety-gate-alerts"],
    )
    item.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    yield item
    item.conn.close()


def test_a_products_model_returns_its_reviewed_notices_with_cited_revisions(env):
    answer = env.store.lookup(
        NS, scopes=h.READ, model_id=env.model("icecat", "EX-32U8")
    )
    assert answer["status"] == "notices on record" and not h.forbidden_keys(answer)
    (notice,) = answer["notices"]
    assert (
        notice["notice_number"] == "SR/00417/26"
        and notice["revision"]["revision_no"] == 2
    )
    assert (
        notice["revision"]["revision_date"] == "2026-04-02"
        and notice["revision_count"] == 2
    )
    connection = notice["connections"][0]
    assert (
        connection["kind"] == "reviewed-product-match"
        and connection["decision"]["decision"] == "accepted"
    )
    assert [a["quoted_text"] for a in notice["corrective_actions"]] == [
        "Recall of the product from end users",
        "Stop of sales",
    ]
    assert (
        notice["corrective_actions"][0]["attribution"]
        == "European Commission (Safety Gate)"
    )
    assert {c["raw"] for c in notice["cited_legal_acts"]} == {
        "Directive 2014/35/EU",
        "Regulation (EU) 2023/988",
    }
    assert [c["raw"] for c in notice["cited_standards"]] == ["EN 62368-1:2014+A11:2017"]
    assert notice["hazards"][0]["hazard_type"] == "Electric shock"
    assert notice["issuing_authority"]["value"] == "eu-safety-gate"
    assert {s["source_id"] for s in answer["sources_consulted"]} == set(
        h.NOTICE_SOURCES
    )


def test_a_revision_published_after_as_of_is_excluded_and_named_as_later(env):
    answer = env.store.lookup(
        NS, scopes=h.READ, model_id=env.model("icecat", "EX-32U8"), as_of="2026-03-20"
    )
    (notice,) = answer["notices"]
    assert (
        notice["revision"]["revision_no"] == 1
        and notice["revision"]["revision_date"] == "2026-03-13"
    )
    assert [r["revision_date"] for r in notice["later_revisions"]] == ["2026-04-02"]
    assert [a["quoted_text"] for a in notice["corrective_actions"]] == [
        "Recall of the product from end users"
    ]
    early = env.store.lookup(
        NS, scopes=h.READ, model_id=env.model("icecat", "EX-32U8"), as_of="2026-03-01"
    )
    assert (
        early["status"] == NO_NOTICE
        and early["later_notices"][0]["note"] == "first published after as_of"
    )
    with pytest.raises(ProductSafetyError) as caught:
        env.store.lookup(NS, scopes=h.READ, gtin="4012345000016", as_of="13/03/2026")
    assert caught.value.code == "invalid_request"


def test_followups_are_dated_and_filtered_by_as_of(env):
    view = env.store.inspect(NS, "safety-gate:SR/00417/26", scopes=h.READ)
    assert view["revision"]["followups"][0]["date"] == "2026-04-02"
    answer = env.store.lookup(
        NS, scopes=h.READ, brand="Exampla", designation="EX-32U8", as_of="2026-04-02"
    )
    notice = next(n for n in answer["notices"] if n["notice_number"] == "SR/00417/26")
    assert [f["country"] for f in notice["followups"]] == ["FR"]


def test_notices_from_different_authorities_stay_side_by_side(env):
    answer = env.store.lookup(NS, scopes=h.READ, gtin="012345678905")
    assert [(n["provider"], n["notice_number"]) for n in answer["notices"]] == [
        ("cpsc", "26117"),
        ("safety-gate", "SR/00388/26"),
    ]
    assert answer["authorities"] == ["eu-safety-gate", "us-cpsc"]
    assert {n["revision"]["revision_id"] for n in answer["notices"]} == {
        env.store.revisions(NS, env.notice("cpsc", "26117"))[0]["revision_id"],
        env.store.revisions(NS, env.notice("safety-gate", "SR/00388/26"))[0][
            "revision_id"
        ],
    }
    # Each keeps its own authority's corrective action, never merged.
    texts = {
        n["provider"]: [a["quoted_text"] for a in n["corrective_actions"]]
        for n in answer["notices"]
    }
    assert texts["cpsc"][0].startswith("Consumers should immediately stop using")
    assert texts["safety-gate"] == ["Recall of the product from end users"]
    assert all(
        c["kind"] == "identification-string"
        for n in answer["notices"]
        for c in n["connections"]
    )


def test_a_product_without_an_accepted_match_has_no_notice_on_record(env):
    for provider, designation in (
        ("icecat", "EX-32U8UK"),
        ("icecat", "EX-34Q4"),
        ("eprel", "EX-32U8"),
    ):
        answer = env.store.lookup(
            NS,
            scopes=h.READ,
            model_id=env.model(provider, designation),
            as_of="2026-09-01",
        )
        assert answer["status"] == NO_NOTICE and answer["notices"] == []
        assert answer["query"]["as_of"] == "2026-09-01" and answer["sources_consulted"]
        assert not h.forbidden_keys(answer)
        assert "not a statement that it is safe" in answer["boundary"]
    sibling = env.store.lookup(
        NS, scopes=h.READ, model_id=env.model("icecat", "EX-32U8UK")
    )
    assert [c["candidate_state"] for c in sibling["unreviewed_candidates"]] == [
        "ambiguous"
    ]
    unknown = env.store.lookup(NS, scopes=h.READ, gtin="4012345000054")
    assert unknown["status"] == NO_NOTICE


def test_a_variant_id_resolves_to_its_model_and_the_gtin_journey_reaches_the_dossier(
    env,
):
    variant = env.conn.execute(
        "SELECT identity_id FROM product_identities WHERE level='variant' AND parent_id=?",
        [env.model("icecat", "EX-27Q4")],
    ).fetchone()[0]
    by_variant = env.store.lookup(NS, scopes=h.READ, variant_id=variant)
    by_gtin = env.store.lookup(NS, scopes=h.READ, gtin="04012345000016")
    assert [n["notice_number"] for n in by_variant["notices"]] == ["SR/00431/26"]
    assert [n["notice_number"] for n in by_gtin["notices"]] == ["SR/00431/26"]
    match = by_gtin["notices"][0]["connections"][0]["product_matches"]
    assert any(
        m["attached"] and m["model_id"] == env.model("icecat", "EX-27Q4") for m in match
    )


def test_invalid_gtins_stay_queryable_as_their_verbatim_string(env):
    answer = env.store.lookup(NS, scopes=h.READ, gtin="4012345000029")
    assert [n["notice_number"] for n in answer["notices"]] == ["SR/00417/26"]
    assert answer["notices"][0]["connections"][0]["matched"]["gtin"] == "4012345000029"


def test_news_is_labelled_reporting_and_needs_its_scope(env):
    env.conn.execute(
        "INSERT INTO documents (document_id, source_type, language, url, title, content, created_at) VALUES "
        "('news-1', 'news', 'en', 'https://news.example.org/kettles', 'Kettle recall', "
        "'Safety Gate alert SR/00388/26 names Brightway kettles.', 1), "
        "('news-2', 'news', 'en', 'https://news.example.org/other', 'Unrelated', 'SR/00388/261 is another code.', 2)"
    )
    with pytest.raises(ProductSafetyError) as caught:
        env.store.lookup(NS, scopes=h.READ, gtin="012345678905", include_news=True)
    assert caught.value.code == "unauthorized"
    answer = env.store.lookup(
        NS, scopes=h.READ | {"knowledge:read"}, gtin="012345678905", include_news=True
    )
    news = {n["notice_number"]: n["news"] for n in answer["notices"]}
    assert [a["document_id"] for a in news["SR/00388/26"]["articles"]] == ["news-1"]
    assert (
        news["SR/00388/26"]["label"]
        == "reporting that names the notice number; not the notice"
    )
    assert news["26117"]["articles"] == []


def test_queries_need_an_identity_and_read_scopes(env):
    with pytest.raises(ProductSafetyError) as caught:
        env.store.lookup(NS, scopes=h.READ)
    assert caught.value.code == "invalid_request"
    with pytest.raises(ProductSafetyError) as caught:
        env.store.lookup(NS, scopes={"namespace:global:read"}, gtin="012345678905")
    assert caught.value.code == "unauthorized"
    with pytest.raises(ProductSafetyError) as caught:
        env.store.lookup(NS, scopes=h.READ, model_id="product-model:unknown")
    assert caught.value.code == "not_found"
