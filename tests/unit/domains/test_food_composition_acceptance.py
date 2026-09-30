"""FC11 (#2297): offline GTIN-to-composition acceptance for the Products food feature.

Pinned synthetic Open Food Facts (two label revisions), FoodData Central
(Branded, Foundation, SR Legacy) and Ciqual fixtures replay through the real
``food-composition`` adapter and the source-pack runtime beside the Products
safety RASFF fixture, with sockets blocked. The journey runs through the MCP
tools: identity review, notice linking, as-of answers, label history and a
subscription event.
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.kb.food_composition import feature_enabled, forbidden_keys
from src.kb.food_notice_links import NO_NOTICE
from tests.unit import food_composition_harness as h
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp import server


@pytest.fixture()
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_gtin_to_cited_composition_label_history_identity_and_linked_notices(offline, tmp_path, monkeypatch):
    # The feature is optional and off until selected in the active composition.
    conn0, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn0) is False
    coordinator.select("products", bundles["products"]["version"], features=["food", "safety"])
    assert coordinator.activate("products-food-on")["status"] == "published" and feature_enabled(conn0)

    path = str(tmp_path / "food-acceptance.duckdb")
    env = h.Env(duckdb.connect(path))
    assert env.run_notices()["status"] == "complete"  # the RASFF notice Products safety already holds
    food_run = env.run_food()
    assert food_run["status"] == "complete"
    # Re-running the same pinned selection adds no revision.
    before = env.conn.execute("SELECT count(*) FROM food_revisions").fetchone()[0]
    assert env.run_food("food-replay")["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM food_revisions").fetchone()[0] == before
    env.conn.close()

    state = {"principal": "analyst", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())

    # Identity: the UPC-A in FDC Branded and the EAN-13 in OFF meet; the other brand's GTIN is a conflict.
    proposed = tools["propose_food_matches"].fn(namespace=h.NS)
    same = next(c for c in proposed["candidates"] if c["basis"] == "gtin" and c["candidate_state"] == "proposed")
    assert proposed["conflicts"] and all(c["review_state"] == "unreviewed" for c in proposed["candidates"])
    refused = tools["review_food_match"].fn(namespace=h.NS, match_id=proposed["conflicts"][0]["match_id"],
                                            decision="accepted", reason="same code")
    assert refused["ok"] is False and refused["error"]["code"] == "conflicting_identifiers"
    accepted = tools["review_food_match"].fn(namespace=h.NS, match_id=same["match_id"], decision="accepted",
                                             reason="GTIN and brand agree")
    assert accepted["accepted"] and accepted["review_history"][0]["principal_id"] == "analyst"

    # Notice linking by citation (brand + exact designation of the RASFF notice).
    linked = tools["link_food_notices"].fn(namespace=h.NS)["linked"]
    assert [(link["basis"], link["state"]) for link in linked] == [("brand+designation", "cited")]

    # A subscription on the GTIN hears its label revisions and the linked notice.
    monitor = tools["create_food_composition_monitor"].fn(namespace=h.NS, request_key="kebab", gtins=[h.KEBAB])
    events = tools["run_food_composition_monitor"].fn(subscription_id=monitor["subscription_id"])
    assert {n["kind"] for n in events["notifications"]} == {"label_revision", "nutrient_value_change",
                                                             "allergen_declaration_change", "new_linked_notice"}
    replay = tools["run_food_composition_monitor"].fn(subscription_id=monitor["subscription_id"])
    assert replay["notifications"] == []

    # The GTIN as of dates: label history, cited values, ODbL attribution, linked notice with quoted hazard.
    early = tools["food_composition_as_of"].fn(namespace=h.NS, gtin=h.KEBAB, as_of="2026-01-15")
    assert early["sources"][0]["citation"]["revision"]["value"] == "4"
    assert early["notices"]["status"] == NO_NOTICE  # the notice was not yet published
    now = tools["food_composition_as_of"].fn(namespace=h.NS, gtin=h.KEBAB, include_history=True)
    (kebab,) = now["sources"]
    assert kebab["provenance_class"] == "crowd-sourced" and kebab["citation"]["revision"]["value"] == "7"
    assert [r["revision"] for r in kebab["revision_history"]] == ["4", "7"]
    assert all(v["cite"]["attribution"]["licence"] == "ODbL-1.0" and v["cite"]["attribution"]["share_alike"]
               for values in kebab["label"].values() for v in values)
    notice = now["notices"]["notices"][0]
    assert notice["notice_number"] == "2026.0457" and notice["link"]["notice_revision_id"]
    assert notice["side_by_side"]["notice_hazards_quoted"][0]["description"] == "Salmonella Enteritidis"
    assert "no causal" in notice["side_by_side"]["note"]
    history = tools["food_label_history"].fn(namespace=h.NS, gtin=h.KEBAB)
    assert [r["revision"] for r in history["histories"][0]["revisions"]] == ["4", "7"]

    # Crowd-sourced and reference side by side, never reconciled; explicit unknowns.
    bar = tools["food_composition_as_of"].fn(namespace=h.NS, gtin="071000000208")
    assert bar["provenance_classes"] == ["crowd-sourced", "reference"]
    assert {e["provider"] for e in bar["sources"]} == {"open-food-facts", "fooddata-central"}
    rows = {r["nutrient"]: r for r in bar["side_by_side_nutrients"]}
    assert rows["protein"]["comparison"] == "differs as published"
    assert rows["sodium"]["comparison"].startswith("not comparable")
    assert any(u["kind"] == "unit_absent" for u in bar["unknowns"])
    assert bar["identity"]["conflicts"]
    assert tools["food_composition_as_of"].fn(namespace=h.NS, gtin=h.UNKNOWN)["status"] == "unmatched_gtin"
    yoghurt = tools["food_composition_as_of"].fn(namespace=h.NS, gtin=h.YOGHURT)
    assert yoghurt["notices"]["status"] == NO_NOTICE and "not a statement" in yoghurt["notices"]["note"]

    # Generic foods: FDC Foundation and Ciqual by reviewed name match, each cited to its revision or edition.
    apple = tools["lookup_food_products"].fn(namespace=h.NS, query="apple", food_kind="generic-food")["foods"]
    assert {f["provider"] for f in apple} == {"fooddata-central", "composition-table"}
    generic = tools["food_composition_as_of"].fn(namespace=h.NS, food="composition-table:ciqual:13039")
    assert generic["sources"][0]["citation"]["revision"] == {"value": "2020", "basis": "table-edition",
                                                             "date": "2020-07-07"}

    for answer in (early, now, bar, yoghurt, generic):
        assert not forbidden_keys(answer)
