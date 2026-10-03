"""TO06 (#2739): tourism series linked to Geospatial boundaries and Labour series by shared code or accepted match."""

from __future__ import annotations

import pytest

from src.kb.tourism_identity import TourismIdentity
from src.kb.tourism_links import TourismLinks
from src.kb.tourism_records import TourismError, forbidden_paths
from tests.unit import labour_harness as lh
from tests.unit import tourism_harness as h


def _loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    return conn


def test_absent_providers_are_reported_not_dropped():
    conn = _loaded()
    links = TourismLinks(conn)
    boundary = links.link_boundaries(h.NS, principal_id="svc", scopes=h.SCOPES)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert boundary["linked"] == [] and labour["linked"] == []
    states = {(link["kind"], link["state"]) for link in links.links(h.NS, scopes=h.READ_ONLY)}
    assert states == {("boundary", "provider_absent"), ("labour", "provider_absent")}
    # One per series, pinned to its latest published vintage (also the removed arrivals series), never dropped.
    current = links.store.find_series(h.NS)
    assert len(links.links(h.NS, scopes=h.READ_ONLY, kind="boundary")) == len(current)


def test_boundaries_link_by_shared_published_code_in_the_series_nuts_version():
    conn = _loaded()
    h.register_boundaries(conn, version="2021", codes=("DE30",))
    h.register_boundaries(conn, version="2024", codes=("DE", "DE30"))
    links = TourismLinks(conn)
    links.link_boundaries(h.NS, principal_id="svc", scopes=h.SCOPES, geo_namespace=h.GEO_NS)
    by_code: dict[tuple[str, str], list[dict]] = {}
    for link in links.links(h.NS, scopes=h.READ_ONLY, kind="boundary"):
        by_code.setdefault((link["reference"]["code"], link["state"]), []).append(link)
    berlin = by_code[("DE30", "linked")][0]
    assert berlin["basis"] == "shared-identifier" and berlin["target"]["collection"] == "gisco:nuts:2021"
    assert berlin["target"]["revision_id"] and berlin["vintage_id"]
    assert berlin["evidence"]["tourism_citation"]["vintage_id"] == berlin["vintage_id"]
    # DE exists only in the 2024 collection; a 2021 key is never looked up in another version's collection.
    assert ("DE", "linked") not in by_code
    assert {link["evidence"]["collection"] for link in by_code[("DE", "target_not_held")]} == {"gisco:nuts:2021"}
    with pytest.raises(TourismError):
        links.link_boundaries(h.NS, principal_id="svc", scopes={"knowledge:tourism:write", "namespace:global:write"})


def test_labour_links_reach_section_i_series_by_shared_code_and_pin_revisions():
    conn = _loaded()
    h.load_labour_accommodation(conn)
    links = TourismLinks(conn)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = links.links(h.NS, scopes=h.READ_ONLY, kind="labour", state="linked")
    assert linked and {link["basis"] for link in linked} == {"shared-identifier"}
    for link in linked:
        assert link["target"]["sector"]["code"] == "I" and link["target"]["vintage_id"]
        assert link["reference"]["code"] == link["evidence"]["code"]["code"]
        assert link["evidence"]["labour_citation"]["vintage_id"] == link["target"]["vintage_id"]
        assert link["evidence"]["tourism_citation"]["vintage_id"] == link["vintage_id"]
        assert forbidden_paths(link) == []  # no ratio, share or per-bed figure
    berlin = next(link for link in linked if link["reference"]["code"] == "DE30")
    assert "2096" in berlin["evidence"]["shared_reference_years"]


def test_labour_links_through_accepted_matches_only():
    from src.kb.labour_identity import LabourIdentity

    # The labour document states its codes under ISO 3166-1 alpha-2, so only reviewed matches of both sides to the
    # same Geospatial place link them.
    conn = _loaded()
    places = h.register_places(conn, keys=("de",))
    identity = TourismIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "code", principal_id="reviewer",
                            scopes=h.SCOPES)
    h.load_labour_accommodation(conn, area_scheme="iso3166-1-alpha2")
    links = TourismLinks(conn)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert links.links(h.NS, scopes=h.READ_ONLY, kind="labour", state="linked") == []  # nothing reviewed yet
    labour_identity = LabourIdentity(conn)
    for assertion in labour_identity.propose_places(h.NS, principal_id="analyst", scopes=lh.SCOPES,
                                                    geo_namespace="global")["assertions"]:
        if assertion["state"] == "proposed":
            labour_identity.review(h.NS, assertion["assertion_id"], "accept", "code", principal_id="reviewer",
                                   scopes=lh.SCOPES)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = links.links(h.NS, scopes=h.READ_ONLY, kind="labour", state="linked")
    assert linked and {link["basis"] for link in linked} == {"accepted-match"}
    assert {link["reference"]["code"] for link in linked} == {"DE"}
    assert linked[0]["evidence"]["place_id"] == places["de"]
    assert linked[0]["evidence"]["tourism_assertion_id"] and linked[0]["evidence"]["labour_assertion_id"]
    # Berlin has no reviewed match on either side: reported, never dropped.
    missing = links.links(h.NS, scopes=h.READ_ONLY, kind="labour", state="target_not_held")
    assert "DE30" in {link["reference"]["code"] for link in missing}


def test_labour_series_of_other_sectors_are_not_linked_and_missing_targets_are_reported():
    conn = _loaded()
    lh.load_all(conn)  # Eurostat LFS for DE and DE30 (unemployment, NACE C), no section I
    TourismLinks(conn).link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    states = {link["state"] for link in TourismLinks(conn).links(h.NS, scopes=h.READ_ONLY, kind="labour")}
    assert states == {"target_not_held"}
    with pytest.raises(TourismError):
        TourismLinks(conn).link_labour(h.NS, principal_id="svc",
                                       scopes={"knowledge:tourism:write", "namespace:global:write"})
