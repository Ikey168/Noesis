"""Places, sectors and occupations across labour sources through reviewable identity (#2472)."""

from __future__ import annotations

import pytest

from src.kb.geospatial import GeospatialStore
from src.kb.labour_identity import LabourIdentity
from src.kb.labour_statistics import LabourError
from tests.unit import labour_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn)
    return conn


def by_code(result):
    return {(a["subject"]["scheme"], a["subject"]["code"]): a for a in result["assertions"]}


def test_area_codes_map_to_places_by_published_code_with_the_mapping_source_cited(loaded):
    places = h.register_places(loaded)
    identity = LabourIdentity(loaded)
    proposed = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo"))
    assert proposed[("iso3166-1-alpha3", "DEU")]["target"]["place_id"] == places["de"]
    assert proposed[("iso3166-1-alpha3", "DEU")]["evidence"]["candidates"][0]["evidence"]["source_id_key"] == \
        "iso3166-1-alpha3"
    assert proposed[("eurostat-geo", "DE")]["method"] == "iso-alpha2-equivalent"
    assert proposed[("eurostat-geo", "DE30")]["target"]["place_id"] == places["be"]
    assert proposed[("bls-laus-area", "ST0600000000000")]["method"] == "fips-from-laus-area"
    assert proposed[("iso3166-1-alpha2", "US")]["target"]["place_id"] == places["us"]
    assert all(a["state"] == "proposed" for a in proposed.values())
    # Nothing is used before review; accepting records reviewer and time.
    assert identity.area_codes_for_place(h.NS, places["de"]) == []
    accepted = identity.review(h.NS, proposed[("iso3166-1-alpha3", "DEU")]["assertion_id"], "accept",
                               "published ISO code", principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["history"][-1]["by"] == "reviewer" and accepted["history"][-1]["at_ms"]
    assert [c["code"] for c in identity.area_codes_for_place(h.NS, places["de"])] == ["DEU"]
    reverted = identity.revert(h.NS, accepted["assertion_id"], "wrong gazetteer", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.area_codes_for_place(h.NS, places["de"]) == []


def test_ambiguous_and_unmatched_areas_stay_review_candidates(loaded):
    places = h.register_places(loaded, keys=("de", "us"))
    GeospatialStore(loaded).register_place(
        "geo", "Deutschland (second gazetteer entry)", "country", names=[{"value": "Deutschland", "language": "de"}],
        source_ids={"iso3166-1-alpha3": "DEU"}, parent_ids=[], principal_id="op",
        scopes={"knowledge:geospatial:write"}, place_key="fixture:de-duplicate")
    identity = LabourIdentity(loaded)
    proposed = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo"))
    deu = proposed[("iso3166-1-alpha3", "DEU")]
    assert deu["state"] == "ambiguous" and deu["target"] is None and len(deu["evidence"]["candidates"]) == 2
    assert proposed[("eurostat-geo", "DE30")]["state"] == "unmatched"
    with pytest.raises(LabourError):
        identity.review(h.NS, deu["assertion_id"], "accept", "choose", principal_id="r", scopes=h.SCOPES,
                        place_id="place:not-a-candidate")
    chosen = identity.review(h.NS, deu["assertion_id"], "accept", "the national gazetteer entry",
                             principal_id="reviewer", scopes=h.SCOPES, place_id=places["de"])
    assert chosen["state"] == "accepted" and chosen["target"]["place_id"] == places["de"]
    # Re-proposing keeps the reviewed decision.
    again = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo"))
    assert again[("iso3166-1-alpha3", "DEU")]["assertion_id"] == deu["assertion_id"]


def test_sectors_and_occupations_map_only_through_published_concordances_with_versions(loaded):
    identity = LabourIdentity(loaded)
    isic = {"scheme": "ISIC", "version": "Rev.4"}
    before = identity.propose_classifications(h.NS, "sector", isic, principal_id="analyst", scopes=h.SCOPES)
    states = {(a["subject"]["scheme"], a["subject"]["code"]): a["state"] for a in before["assertions"]}
    assert states == {("ISIC", "C"): "proposed", ("NACE", "C"): "unmatched", ("NAICS", "31-33"): "unmatched"}
    h.import_concordances(loaded)
    sectors = {(a["subject"]["scheme"], a["subject"]["code"]): a for a in identity.propose_classifications(
        h.NS, "sector", isic, principal_id="analyst", scopes=h.SCOPES)["assertions"]}
    assert sectors[("ISIC", "C")]["method"] == "same-classification" and sectors[("ISIC", "C")]["relation"] == "exact"
    assert sectors[("NACE", "C")]["relation"] == "exact"
    assert sectors[("NACE", "C")]["subject"]["version"] == "Rev.2" and sectors[("NACE", "C")]["subject"][
        "target"] == isic
    naics = sectors[("NAICS", "31-33")]
    assert naics["relation"] == "partial" and naics["target"]["codes"][0]["concordance"]["citation"]["publisher"] \
        == "US Census Bureau"
    for assertion in sectors.values():
        identity.review(h.NS, assertion["assertion_id"], "accept", "published table", principal_id="reviewer",
                        scopes=h.SCOPES)
    codes = identity.codes_for(h.NS, "sector", {**isic, "code": "C"})
    assert {(c["scheme"], c["relation"]) for c in codes} == {("ISIC", "exact"), ("NACE", "exact"),
                                                             ("NAICS", "partial")}
    occupations = {(a["subject"]["scheme"], a["subject"]["code"]): a for a in identity.propose_classifications(
        h.NS, "occupation", {"scheme": "ISCO", "version": "08"}, principal_id="analyst",
        scopes=h.SCOPES)["assertions"]}
    assert occupations[("SOC", "15-1252")]["target"]["codes"][0]["code"] == "2512"
    assert occupations[("ISCO", "2")]["relation"] == "exact"
    with pytest.raises(LabourError):
        identity.import_concordance(h.NS, {"label": "x", "source": {"scheme": "SOC", "version": "2018"},
                                           "target": {"scheme": "ISCO", "version": "08"}, "citation": {},
                                           "rows": []}, principal_id="op", scopes=h.SCOPES)
    with pytest.raises(LabourError):
        identity.propose_classifications(h.NS, "sector", isic, principal_id="a", scopes=h.READ_ONLY)
