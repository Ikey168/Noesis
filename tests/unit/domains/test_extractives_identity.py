"""Companies, commodities, countries and projects through reviewable identity (#2682)."""

from __future__ import annotations

import pytest

from src.kb.extractives_identity import ExtractivesIdentity
from src.kb.extractives_records import ExtractivesError
from tests.unit import extractives_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    h.import_concordances(conn)
    return conn, ExtractivesIdentity(conn)


def test_companies_are_offered_by_published_identifier_first_and_never_accepted_automatically(env):
    conn, identity = env
    h.ownership(conn)
    result = identity.propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    views = result["candidates"]
    assert views and {v["state"] for v in views} == {"proposed"}
    methods = {v["method"] for v in views}
    assert "exact-identifier" in methods
    assert all(v["low_evidence"] == (v["method"] == "name-jurisdiction") for v in views)
    lei = next(v for v in views if v["ownership_key"] == h.INT_ENTITY)
    assert lei["method"] == "exact-identifier" and lei["evidence"][0]["identifiers"][0]["scheme"] == "lei"
    # Nothing is merged: every reporting company stays unmatched until a reviewer decides.
    names = {u["name_as_reported"] for u in result["unmatched"]}
    assert {"Andes Cobre S.A. (fixture)", "[natural person - redacted]"} <= names
    assert not any(v["subject_key"] == u["key"] for v in views for u in result["unmatched"] if u["redacted"])
    accepted = identity.review_company(h.NS, lei["candidate_id"], "accept", "LEI agrees", principal_id="reviewer",
                                       scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["decision_id"]
    assert "Exampla Intermediate B.V." not in {u["name_as_reported"] for u in identity.unmatched_companies(
        h.NS, scopes=h.SCOPES)}
    reverted = identity.revert_company(h.NS, lei["candidate_id"], "wrong register", principal_id="reviewer",
                                       scopes=h.SCOPES)
    assert reverted["state"] == "reverted"
    assert "Exampla Intermediate B.V." in {u["name_as_reported"] for u in identity.unmatched_companies(
        h.NS, scopes=h.SCOPES)}
    # Re-proposing never changes a reviewed candidate silently and adds no duplicate.
    again = identity.propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert len(again["candidates"]) == len(views)


def test_without_the_ownership_store_companies_stay_unmatched(env):
    _, identity = env
    result = identity.propose_companies(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert result["candidates"] == [] and len(result["unmatched"]) == 4


def test_commodities_use_the_stated_hs_code_first_then_a_cited_concordance(env):
    _, identity = env
    assertions = identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]
    by = {(a["subject"]["provider"], a["subject"]["name"]): a for a in assertions}
    stated = by[("eiti", "Copper ores and concentrates")]
    assert stated["method"] == "published-hs-code" and stated["target"]["codes"][0]["code"] == "260300"
    usgs = by[("usgs-mcs", "Copper")]
    assert usgs["method"] == "published-concordance" and usgs["relation"] == "partial"
    assert usgs["target"]["codes"][0]["concordance"]["citation"]["file_sha256"]
    assert all(a["confidence"] and a["evidence"] is not None for a in assertions if a["state"] == "proposed")
    # Nothing is used before review: an HS query finds no series yet.
    assert identity.commodity_keys_for(h.NS, {"hs_code": "2603"})["keys"] == []
    h.review_all(identity, assertions)
    keys = identity.commodity_keys_for(h.NS, {"hs_code": "2603"})["keys"]
    assert {k["provider"] for k in keys} == {"usgs-mcs", "bgs-wms", "eiti"}
    assert {k["relation"] for k in keys if k["provider"] == "eiti"} == {"narrower"}


def test_countries_use_published_codes_before_names_and_aggregates_stay_unmatched(env):
    _, identity = env
    assertions = identity.propose_countries(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]
    by = {(a["subject"]["provider"], a["subject"]["name"]): a for a in assertions}
    assert by[("bgs-wms", "Peru")]["method"] == "published-code"
    assert by[("usgs-mcs", "Peru")]["method"] == "published-code-list"
    world = by[("usgs-mcs", "World total (rounded)")]
    assert world["state"] == "unmatched" and "aggregate" in world["reason"]
    h.review_all(identity, assertions)
    names = identity.country_names_for(h.NS, "PER")["names"]
    assert {(n["provider"], n["name"]) for n in names} == {("usgs-mcs", "Peru"), ("bgs-wms", "Peru")}


def test_projects_match_assets_by_published_identifier_or_coordinates_only(env):
    conn, identity = env
    empty = identity.propose_projects(h.NS, infra_namespace=h.INFRA_NS, principal_id="a", scopes=h.SCOPES)
    assert {a["state"] for a in empty["assertions"]} == {"unmatched"}
    assets = h.seed_infrastructure(conn)
    fresh = ExtractivesIdentity(conn)
    result = fresh.propose_projects(h.NS, infra_namespace=h.INFRA_NS, principal_id="a", scopes=h.SCOPES)
    by = {a["subject"]["name_as_reported"]: a for a in result["assertions"]}
    cerro = by["Cerro Ejemplo (fixture)"]
    assert cerro["method"] == "published-identifier" and cerro["target"]["asset_id"] == assets["M-FIX-1"]
    tajo = by["Tajo Norte (fixture)"]
    assert tajo["method"] == "published-coordinates" and tajo["low_evidence"]
    assert tajo["target"]["asset_id"] == assets["M-FIX-2"] and tajo["evidence"]["distance_m"] <= tajo["evidence"][
        "tolerance_m"]
    with pytest.raises(ExtractivesError):
        fresh.propose_projects(h.NS, infra_namespace=h.INFRA_NS, principal_id="a", scopes={
            "knowledge:extractives:write", "namespace:global:write"})


def test_review_requires_a_reason_and_only_proposals_are_reviewed(env):
    _, identity = env
    (first, *_) = identity.propose_countries(h.NS, principal_id="a", scopes=h.SCOPES)["assertions"]
    with pytest.raises(ExtractivesError):
        identity.review(h.NS, first["assertion_id"], "accept", " ", principal_id="r", scopes=h.SCOPES)
    world = next(a for a in identity.assertions(h.NS, scopes=h.SCOPES, kind="country") if a["state"] == "unmatched")
    with pytest.raises(ExtractivesError):
        identity.review(h.NS, world["assertion_id"], "accept", "x", principal_id="r", scopes=h.SCOPES)
    with pytest.raises(ExtractivesError):
        identity.review(h.NS, first["assertion_id"], "accept", "x", principal_id="r", scopes=h.READ_ONLY)
