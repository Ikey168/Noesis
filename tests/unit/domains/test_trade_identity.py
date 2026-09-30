"""Reviewable identity for reporter/partner areas and product codes across classification vintages (#2545)."""

from __future__ import annotations

import pytest

from src.kb.geospatial import GeospatialStore
from src.kb.trade_flows import TradeError, TradeFlowStore
from src.kb.trade_identity import TradeIdentity, special_area
from tests.unit import trade_harness as h

GEO_WRITE = {"knowledge:geospatial:write", "knowledge:geospatial:read"}


def register_places(conn, *, ambiguous_china=False):
    places = GeospatialStore(conn)
    ids = {}
    for key, name, source_ids in (
        ("de", "Germany", {"m49": "276", "iso3166-1-alpha3": "DEU", "eurostat-geo": "DE"}),
        ("cn", "China", {"m49": "156", "iso3166-1-alpha3": "CHN"}),
        ("fr", "France", {"iso3166-1-alpha2": "FR"}),
    ):
        ids[key] = places.register_place(
            "geo", name, "country", names=[{"value": name, "language": "en"}], source_ids=source_ids,
            parent_ids=[], principal_id="op", scopes=GEO_WRITE, place_key=f"fixture:{key}",
        )["place_id"]
    if ambiguous_china:
        places.register_place(
            "geo", "China (duplicate register entry)", "country", names=[{"value": "China", "language": "en"}],
            source_ids={"m49": "156"}, parent_ids=[], principal_id="op", scopes=GEO_WRITE, place_key="fixture:cn2",
        )
    return ids


def add_special_partner(conn):
    """A synthetic Comtrade report to 'Areas, n.e.s.' (899), stored beside the fixture reports."""
    records = h.fetch("comtrade")[0]
    header = {**records[0]["trade_release"], "file_sha256": "9" * 64, "item_count": 1}
    item = dict(records[0]["trade_item"])
    item["partner"] = {"scheme": "m49", "code": "899", "label": "Areas, nes"}
    TradeFlowStore(conn).apply_release(h.NS, header, [item], run_id="r", source_id="un-comtrade-trade-flows")


def by_code(result):
    return {a["subject"]["code"]: a for a in result["assertions"]}


def test_area_codes_resolve_to_places_by_method_with_evidence_and_special_areas_stay_distinct():
    conn = h.connection()
    h.load_all(conn)
    add_special_partner(conn)
    ids = register_places(conn)
    identity = TradeIdentity(conn)
    result = identity.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="geo")
    areas = by_code(result)
    assert areas["276"]["method"] == "published-code" and areas["276"]["target"]["place_id"] == ids["de"]
    assert areas["DE"]["target"]["place_id"] == ids["de"] and areas["DE"]["method"] == "published-code"
    assert areas["FR"]["method"] == "iso-alpha2-equivalent" and areas["FR"]["evidence"]["strength"] == "medium"
    assert areas["156"]["state"] == "proposed" and areas["156"]["evidence"]["candidates"]
    special = areas["899"]
    assert special["state"] == "special-area" and special["target"] is None
    assert special["reason"] == "not-elsewhere-specified"
    assert special_area("m49", "810")["kind"] == "former-country"
    assert special_area("eurostat-geo", "EU27_2020")["kind"] == "customs-union"
    # Idempotent: proposing again creates nothing.
    assert identity.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="geo")["created"] == []


def test_matches_are_reviewed_accepted_or_reverted_and_only_accepted_ones_join_codes():
    conn = h.connection()
    h.load_all(conn)
    ids = register_places(conn)
    identity = TradeIdentity(conn)
    areas = by_code(identity.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="geo"))
    assert identity.equivalent_codes(h.NS, "276")["codes"] == ["276"]  # nothing accepted yet
    identity.review(h.NS, areas["276"]["assertion_id"], "accept", "M49 code", principal_id="bob", scopes=h.SCOPES)
    accepted = identity.review(h.NS, areas["DE"]["assertion_id"], "accept", "GEO code", principal_id="bob",
                               scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["history"][-1]["by"] == "bob"
    joined = identity.equivalent_codes(h.NS, "DE")
    assert joined["codes"] == ["276", "DE"] and joined["place_ids"] == [ids["de"]]
    assert {b["method"] for b in joined["basis"]} == {"published-code"}
    with pytest.raises(TradeError) as caught:
        identity.review(h.NS, areas["DE"]["assertion_id"], "accept", "again", principal_id="bob", scopes=h.SCOPES)
    assert caught.value.code == "invalid_state"
    with pytest.raises(TradeError) as caught:
        identity.review(h.NS, areas["FR"]["assertion_id"], "accept", "x", principal_id="bob", scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"
    reverted = identity.revert(h.NS, areas["DE"]["assertion_id"], "wrong register", principal_id="bob",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted"
    assert identity.equivalent_codes(h.NS, "DE")["codes"] == ["DE"]
    # A reverted decision is not silently re-proposed on unchanged evidence.
    again = by_code(identity.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="geo"))
    assert again["DE"]["state"] == "reverted"


def test_ambiguous_and_unknown_codes_stay_unmatched_and_visible():
    conn = h.connection()
    h.load_all(conn)
    register_places(conn, ambiguous_china=True)
    identity = TradeIdentity(conn)
    areas = by_code(identity.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="geo"))
    assert areas["156"]["state"] == "unmatched" and "more than one" in areas["156"]["reason"]
    candidates = areas["156"]["evidence"]["candidates"]
    assert len({c["place_id"] for c in candidates if c["method"] == "published-code"}) == 2
    # The built-in gazetteer's equal name is kept as a weak candidate, never deciding on its own.
    assert {c["strength"] for c in candidates if c["method"] == "gazetteer-name"} == {"weak"}
    empty = TradeIdentity(h.connection())
    conn2 = empty.conn
    h.apply(conn2, "comext", retrieved_at_ms=h.FIRST_RETRIEVAL)
    lone = by_code(empty.propose_areas(h.NS, principal_id="alice", scopes=h.SCOPES, geo_namespace="nowhere"))
    assert lone["DE"]["state"] == "unmatched" and lone["DE"]["reason"] == "no place carries this code"
    unmatched = empty.assertions(h.NS, scopes=h.READ_ONLY, kind="area", state="unmatched")
    assert {a["subject"]["code"] for a in unmatched} == {"DE", "FR"}


def test_product_codes_resolve_across_vintages_citing_the_concordance_and_flagging_non_exact_mappings():
    conn = h.connection()
    h.load_all(conn)
    identity = TradeIdentity(conn)
    result = identity.propose_products(h.NS, {"scheme": "HS", "vintage": "HS2022"}, principal_id="alice",
                                       scopes=h.SCOPES)
    products = {(a["subject"]["code"], a["subject"]["vintage"]): a for a in result["assertions"]}
    hs2017 = products[("854140", "HS2017")]
    assert hs2017["state"] == "proposed" and hs2017["method"] == "concordance"
    assert hs2017["evidence"]["exact"] is False
    targets = hs2017["target"]["targets"]
    assert {t["code"] for t in targets} == {"854141", "854142", "854143", "854149"}
    assert all(t["concordance"]["label"].startswith("WITS concordance HS 2022") for t in targets)
    assert all(t["mapping_type"] == "1:n" and t["exact"] is False for t in targets)
    cn8 = products[("85414300", "CN2099")]
    assert cn8["method"] == "cn-structure" and cn8["target"]["targets"][0]["code"] == "854143"
    assert "not stated" in cn8["target"]["targets"][0]["cn_hs_edition"]
    exact = identity.resolve_product(h.NS, "293090", {"scheme": "HS", "vintage": "HS2017"},
                                     {"scheme": "HS", "vintage": "HS2022"})
    assert exact["exact"] is True and exact["targets"][0]["mapping_type"] == "1:1"
    none = identity.resolve_product(h.NS, "293090", {"scheme": "HS", "vintage": "HS2012"},
                                    {"scheme": "HS", "vintage": "HS2022"})
    assert none["targets"] == [] and none["reason"].startswith("no concordance")
    reviewed = identity.review(h.NS, hs2017["assertion_id"], "reject", "split code; keep separate",
                               principal_id="bob", scopes=h.SCOPES)
    assert reviewed["state"] == "rejected"
    rerun = identity.propose_products(h.NS, {"scheme": "HS", "vintage": "HS2022"}, principal_id="alice",
                                      scopes=h.SCOPES)
    assert rerun["created"] == []
