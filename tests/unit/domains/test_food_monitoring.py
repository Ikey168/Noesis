"""FC09 (#2286): label revisions, composition updates, table editions and linked notices through subscriptions."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.food_composition_sources import fixture_transport
from src.kb.food_composition import FoodCompositionError
from src.kb.food_monitoring import FoodCompositionMonitor
from src.kb.food_notice_links import FoodNoticeLinks
from tests.unit import food_composition_harness as h

NS = h.NS


def off_source(selection):
    source = copy.deepcopy(h.source("off-food-products"))
    source["food_composition"]["selection"] = selection
    return source


@pytest.fixture()
def env():
    env = h.Env()
    env.monitor = FoodCompositionMonitor(env.conn, now=lambda: next(env.clock))
    return env


def refresh(env, source, pages):
    return env.monitor.refresh(NS, source, principal_id="op", scopes=h.ALL, transport=fixture_transport(pages),
                               secret="fixture-credential")


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_each_event_type_cites_both_revisions_and_replays_emit_nothing(env):
    pages = h.pages("off-food-products")
    first = refresh(env, off_source([{"gtin": h.KEBAB, "rev": 4}]), pages)
    assert first["status"] == "complete" and first["new_revisions"] == 1 and first["receipt_id"]
    created = env.monitor.create(NS, "kebab", watch={"gtins": [h.KEBAB]}, principal_id="op", scopes=h.ALL)
    subscription = created["subscription_id"]
    initial = env.monitor.run(subscription, principal_id="op", scopes=h.ALL)
    assert kinds(initial) == ["label_revision"]
    assert initial["notifications"][0]["cites"]["revision"] == "4"
    assert env.monitor.run(subscription, principal_id="op", scopes=h.ALL)["notifications"] == []  # replay
    second = refresh(env, off_source([{"gtin": h.KEBAB}]), pages)
    assert second["new_revisions"] == 1
    update = env.monitor.run(subscription, principal_id="op", scopes=h.ALL)
    assert kinds(update) == ["allergen_declaration_change", "label_revision", "nutrient_value_change"]
    by_kind = {n["kind"]: n for n in update["notifications"]}
    revisions = {r["revision_value"]: r["revision_id"] for r in
                 env.food.revisions(NS, env.food_id("open-food-facts", f"off:{h.KEBAB}"))}
    for notification in by_kind.values():
        assert notification["cites"]["revision_id"] == revisions["7"]
        assert notification["previous_revision_id"] == revisions["4"]
    assert {"relation": "contains", "value": "en:soybeans"} in by_kind["allergen_declaration_change"]["added"]
    protein = next(c for c in by_kind["nutrient_value_change"]["changes"] if c["nutrient"]["id"] == "proteins"
                   and c["basis"] == "per 100 g")
    assert protein["before"]["amount"] == "18" and protein["after"]["amount"] == "19"
    # An unchanged payload adds no revision and emits nothing.
    third = refresh(env, off_source([{"gtin": h.KEBAB}]), pages)
    assert third["new_revisions"] == 0 and third["unchanged"] == 1
    assert env.monitor.run(subscription, principal_id="op", scopes=h.ALL)["notifications"] == []
    # A newly linked notice.
    assert env.run_notices()["status"] == "complete"
    FoodNoticeLinks(env.conn).link(NS, scopes=h.WRITE, principal_id="linker")
    linked = env.monitor.run(subscription, principal_id="op", scopes=h.ALL)
    assert kinds(linked) == ["new_linked_notice"]
    cites = linked["notifications"][0]["cites"]
    assert cites["notice_id"] == env.notice("rasff", "2026.0457") and cites["notice_revision_id"]
    assert cites["revision_id"] == revisions["7"]
    polled = env.monitor.poll(subscription, principal_id="op", scopes=h.ALL)
    assert len(polled["events"]) >= 5


def test_table_editions_and_generic_food_targets(env):
    table = copy.deepcopy(h.source("ciqual-composition"))
    pages = h.pages("ciqual-composition")
    refresh(env, table, pages)
    monitor = env.monitor.create(NS, "ciqual", watch={"table_editions": ["ciqual"],
                                                      "foods": ["composition-table:ciqual:13039"]},
                                 principal_id="op", scopes=h.ALL)
    first = env.monitor.run(monitor["subscription_id"], principal_id="op", scopes=h.ALL)
    assert kinds(first) == ["label_revision", "new_table_edition"]
    newer = copy.deepcopy(table)
    newer["food_composition"]["selection"][0].update({"edition": "2025", "edition_date": "2025-11-05"})
    pages[0]["zip_files"] = {k: v.replace("<teneur>0,25</teneur>", "<teneur>0,3</teneur>")
                             for k, v in pages[0]["zip_files"].items()}
    refresh(env, newer, pages)
    second = env.monitor.run(monitor["subscription_id"], principal_id="op", scopes=h.ALL)
    assert kinds(second) == ["label_revision", "new_table_edition", "nutrient_value_change"]
    edition = next(n for n in second["notifications"] if n["kind"] == "new_table_edition")
    assert edition["cites"]["edition"] == "2025" and edition["previous_edition"] == "2020"


def test_invalid_watches_and_rate_limits_are_refused(env):
    with pytest.raises(FoodCompositionError):
        env.monitor.create(NS, "bad", watch={"gtins": ["4000000000106"]}, principal_id="op", scopes=h.ALL)
    with pytest.raises(FoodCompositionError):
        env.monitor.create(NS, "bad", watch={"table_editions": ["nosuch"]}, principal_id="op", scopes=h.ALL)
    limited = h.pages("off-food-products")
    for page in limited:
        page.update({"status": 429, "headers": {"Retry-After": "120"}})
    stopped = refresh(env, off_source([{"gtin": h.KEBAB}]), limited)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    waiting = refresh(env, off_source([{"gtin": h.KEBAB}]), h.pages("off-food-products"))
    assert waiting["status"] == "rate_limited_wait" and waiting["pages"] == []
