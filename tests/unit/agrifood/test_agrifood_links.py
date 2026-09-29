"""AF08: agri-food series linked to trade flows, climate/weather and RASFF notices by citation only (#2356)."""

from __future__ import annotations

import pytest

from src.kb.agrifood_identity import AgrifoodIdentity
from src.kb.agrifood_links import AgrifoodLinks
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    identity = AgrifoodIdentity(item.conn, now=item.tick)
    identity.register_places(principal_id="curator", scopes=h.ALL)
    identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    item.identity = identity
    yield item
    item.conn.close()


def _crosswalk(identity, a, b):
    return next(c for c in identity.crosswalks(NS, scopes=h.READ)
                if {(c["left"]["scheme"], c["left"]["code"]), (c["right"]["scheme"], c["right"]["code"])} == {a, b})


TRADE = [
    {"record_id": "trade:us-maize-2023", "kind": "trade-flow", "text": "US exports of maize (HS 1005), 2023",
     "cites": {"commodities": [{"scheme": "hs", "code": "1005"}], "places": [{"scheme": "iso3166-1", "code": "US"}],
               "periods": ["2023"]}},
    {"record_id": "trade:us-maize-psd", "kind": "trade-flow", "text": "US corn exports as PSD reports them",
     "cites": {"commodities": [{"scheme": "psd-commodity", "code": "0440000"}],
               "places": [{"scheme": "psd-country", "code": "US"}]}},
    {"record_id": "trade:fr-wheat", "kind": "trade-flow", "text": "French wheat exports (HS 1001)",
     "cites": {"commodities": [{"scheme": "hs", "code": "1001"}], "places": [{"scheme": "iso3166-1", "code": "FR"}]}},
]


def test_trade_links_need_a_reviewed_crosswalk_and_a_place(env):
    links = AgrifoodLinks(env.conn, now=env.tick)
    unavailable = links.link_trade_flows(NS, None, scopes=h.WRITE, principal_id="linker")
    assert unavailable["status"] == "provider_unavailable" and unavailable["linked"] == []
    maize = env.identity.propose_manual(NS, "hs:1005", "faostat-item:56", "equivalent", "HS 1005 is maize (corn)",
                                        principal_id="rev", scopes=h.REVIEW)
    env.identity.review(NS, maize["crosswalk_id"], "accept", "heading matches", principal_id="rev", scopes=h.REVIEW)
    wheat = env.identity.propose_manual(NS, "hs:1001", "faostat-item:15", "equivalent", "HS 1001 is wheat and meslin",
                                        principal_id="rev", scopes=h.REVIEW)
    env.identity.review(NS, wheat["crosswalk_id"], "reject", "meslin is not wheat", principal_id="rev",
                        scopes=h.REVIEW)
    result = links.link_trade_flows(NS, lambda: TRADE, scopes=h.WRITE, principal_id="linker")
    by_record = {}
    for link in result["linked"]:
        by_record.setdefault(link["target_id"], []).append(link)
    cited = by_record["trade:us-maize-2023"]
    assert {(tuple(x["commodity"]), x["basis"]) for x in cited} == {(("faostat-item", "56"), "reviewed-crosswalk")}
    assert by_record["trade:us-maize-psd"][0]["basis"] == "shared-code"
    assert "trade:fr-wheat" not in by_record  # rejected mapping: no link
    (skipped,) = [s for s in result["skipped"] if s["record_id"] == "trade:fr-wheat"]
    assert skipped["mappings"][0]["state"] == "rejected"
    stored = links.links(NS, scopes=h.READ, commodities=[("faostat-item", "56")])
    assert all(x["citing_text"] and x["claim"].startswith("citation only") for x in stored)
    assert {x["crosswalk_id"] for x in stored if x["basis"] == "reviewed-crosswalk"} == {maize["crosswalk_id"]}
    again = links.link_trade_flows(NS, lambda: TRADE, scopes=h.WRITE, principal_id="linker")
    assert again["linked"] == []  # idempotent


def test_climate_and_weather_links_only_with_explicit_place_and_period(env):
    links = AgrifoodLinks(env.conn, now=env.tick)
    records = [
        {"record_id": "env:iowa-drought-2023", "kind": "drought-summary", "text": "Iowa growing season 2023 summary",
         "cites": {"places": [{"scheme": "us-fips", "code": "19"}], "periods": ["2023"]}},
        {"record_id": "env:iowa-no-period", "kind": "station-series", "text": "Ames station precipitation",
         "cites": {"places": [{"scheme": "us-fips", "code": "19"}]}},
        {"record_id": "env:elsewhere", "kind": "station-series", "text": "Somewhere else",
         "cites": {"places": [{"scheme": "iso3166-1", "code": "BR"}], "periods": ["2023"]}},
    ]
    result = links.link_environment(NS, "weather", lambda: records, scopes=h.WRITE, principal_id="linker")
    assert [(x["target_id"], x["period"], x["basis"]) for x in result["linked"]] == [
        ("env:iowa-drought-2023", "2023", "cited-place-and-period")]
    reasons = {s["record_id"]: s["reason"] for s in result["skipped"]}
    assert "period" in reasons["env:iowa-no-period"] and "place" in reasons["env:elsewhere"]
    (link,) = links.links(NS, scopes=h.READ, owner="weather")
    assert link["commodity"] is None and link["place"] == {"scheme": "us-fips", "code": "19"}
    # Without environment records the provider is unavailable rather than silently empty.
    assert links.link_environment(NS, scopes=h.ALL, principal_id="linker")["status"] == "provider_unavailable"


def test_rasff_notices_link_by_explicit_commodity_mention(env):
    env.seed_rasff()
    links = AgrifoodLinks(env.conn, now=env.tick)
    result = links.link_rasff(NS, scopes=h.ALL, principal_id="linker")
    linked = {(x["target_id"], tuple(x["commodity"])) for x in result["linked"]}
    targets = {x["target_id"] for x in result["linked"]}
    assert len(targets) == 2 and result["unmatched_notices"] == ["2026.1203"]  # sesame paste names no commodity
    assert any(c == ("faostat-item", "56") for _, c in linked)  # "maize"
    assert any(c == ("faostat-item", "15") for _, c in linked)  # "wheat" in "organic wheat flour"
    stored = links.links(NS, scopes=h.READ, owner="products-rasff")
    assert all(x["basis"] == "explicit-commodity-mention" and x["citing_text"] for x in stored)
    assert all("notice_number" in x["locator"] for x in stored)
