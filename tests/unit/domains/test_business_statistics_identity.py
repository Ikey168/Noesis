"""IB06 (#2738): reviewable place matching and NACE/NAICS candidate links through published concordances."""

from __future__ import annotations

import pytest

from src.kb.business_statistics_identity import BusinessIdentity
from src.kb.business_statistics_records import BusinessError
from src.kb.labour_identity import LabourIdentity
from tests.unit import business_statistics_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn)
    return conn


def _by_subject(result, **subject):
    return next(a for a in result["assertions"] if all(a["subject"].get(k) == v for k, v in subject.items()))


def test_places_match_by_published_code_only_and_wait_for_review(loaded):
    places = h.register_places(loaded)
    identity = BusinessIdentity(loaded)
    with pytest.raises(BusinessError):
        identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES - {"knowledge:geospatial:read"})
    result = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    germany = _by_subject(result, scheme="eurostat-geo", code="DE")
    california = _by_subject(result, scheme="us-fips-state", code="06")
    assert germany["state"] == california["state"] == "proposed"
    assert germany["method"] == "iso-alpha2-equivalent" and germany["target"]["place_id"] == places["de"]
    assert california["method"] == "published-code" and california["target"]["place_id"] == places["ca"]
    # Nothing is used before a reviewer accepts it.
    assert identity.place_for_area(h.NS, "us-fips-state", "06") is None
    accepted = identity.review(h.NS, california["assertion_id"], "accept", "FIPS state code", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["history"][-1]["by"] == "reviewer"
    assert identity.place_for_area(h.NS, "us-fips-state", "06")["place_id"] == places["ca"]
    assert [c["code"] for c in identity.area_codes_for_place(h.NS, places["ca"])] == ["06"]
    reverted = identity.revert(h.NS, california["assertion_id"], "wrong review", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.place_for_area(h.NS, "us-fips-state", "06") is None
    # Idempotent: proposing again neither duplicates nor resurrects the reviewed state.
    again = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    assert again["created"] == [] and len(identity.assertions(h.NS, scopes=h.READ_ONLY, kind="area")) == 2


def test_an_unregistered_place_stays_unmatched_and_names_are_never_a_match(loaded):
    h.register_places(loaded, keys=("de",))
    identity = BusinessIdentity(loaded)
    result = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    california = _by_subject(result, scheme="us-fips-state", code="06")
    assert california["state"] == "unmatched" and california["target"] is None
    assert california in identity.unmatched(h.NS, scopes=h.READ_ONLY)


def test_nace_and_naics_are_linked_only_as_reviewable_candidates_through_published_concordances(loaded):
    identity = BusinessIdentity(loaded)
    nothing = identity.propose_classification_links(h.NS, principal_id="proposer", scopes=h.SCOPES)
    assert nothing["assertions"] == [] and len(nothing["unlinked_codes"]) == 7
    tables = h.concordance_tables()
    # The NACE-ISIC and NAICS-ISIC tables are the labour track's LB07 imports, read where they are held.
    labour_identity = LabourIdentity(loaded)
    for table in tables[:2]:
        labour_identity.import_concordance(h.NS, table, principal_id="op", scopes={"knowledge:labour:write",
                                                                                "namespace:global:write"})
    identity.import_concordance(h.NS, tables[2], principal_id="op", scopes=h.SCOPES)
    with pytest.raises(BusinessError):
        identity.import_concordance(h.NS, {**tables[2], "citation": {"url": "https://example.org"}},
                                    principal_id="op", scopes=h.SCOPES)
    result = identity.propose_classification_links(h.NS, principal_id="proposer", scopes=h.SCOPES)
    links = {frozenset(f"{c['scheme']} {c['version']} {c['code']}" for c in a["subject"]["pair"]): a
             for a in result["assertions"]}
    assert set(links) == {frozenset({"NACE Rev.2 C", "NAICS 2022 31-33"}),
                          frozenset({"NAICS 2017 31-33", "NAICS 2022 31-33"}),
                          frozenset({"NAICS 2017 00", "NAICS 2022 00"})}
    nace_naics = links[frozenset({"NACE Rev.2 C", "NAICS 2022 31-33"})]
    assert nace_naics["method"] == "published-concordance-via-pivot" and nace_naics["relation"] == "partial"
    (path,) = nace_naics["evidence"]["paths"]
    assert path["via"] == {"scheme": "ISIC", "version": "Rev.4", "code": "C"}
    assert {leg["concordance"]["held_by"] for leg in path["legs"]} == {"economics.labour (LB07 operator import)"}
    assert all(leg["concordance"]["citation"]["file_sha256"] for leg in path["legs"])
    vintages = links[frozenset({"NAICS 2017 31-33", "NAICS 2022 31-33"})]
    assert vintages["method"] == "published-concordance" and vintages["relation"] == "exact"
    assert nace_naics["target"]["merge"] is False and nace_naics["state"] == "proposed"
    unlinked = {f"{c['scheme']} {c['version']} {c['code']}" for c in result["unlinked_codes"]}
    assert {"NACE Rev.2 B-D", "NACE Rev.2 B-S_X_O_S94"} <= unlinked
    # Only accepted candidates are used; rejecting and reverting are recorded.
    assert identity.linked_codes(h.NS, {"scheme": "NACE", "version": "Rev.2", "code": "C"}) == []
    identity.review(h.NS, nace_naics["assertion_id"], "accept", "published concordances via ISIC Rev.4",
                    principal_id="reviewer", scopes=h.SCOPES)
    (linked,) = identity.linked_codes(h.NS, {"scheme": "NACE", "version": "Rev.2", "code": "C"})
    assert linked["code"] == "31-33" and linked["version"] == "2022" and linked["relation"] == "partial"
    identity.review(h.NS, vintages["assertion_id"], "reject", "keep vintages apart for now", principal_id="reviewer",
                    scopes=h.SCOPES)
    assert identity.linked_codes(h.NS, {"scheme": "NAICS", "version": "2017", "code": "31-33"}) == []
    with pytest.raises(BusinessError):
        identity.review(h.NS, vintages["assertion_id"], "accept", "again", principal_id="reviewer", scopes=h.SCOPES)
    # Series keep their own classification: nothing was merged or re-classified.
    stored = {(s["classification"]["scheme"], s["classification"]["version"]) for s in
              identity.store.find_series(h.NS)}
    assert stored == {("NACE", "Rev.2"), ("NAICS", "2017"), ("NAICS", "2022")}


def test_reviews_need_the_review_scope(loaded):
    h.register_places(loaded)
    identity = BusinessIdentity(loaded)
    result = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    with pytest.raises(BusinessError) as caught:
        identity.review(h.NS, result["assertions"][0]["assertion_id"], "accept", "x", principal_id="r",
                        scopes={"knowledge:business:write", "namespace:global:write"})
    assert caught.value.code == "unauthorized"
