"""AF07: commodity crosswalks and place resolution through reviewable identity (#2353)."""

from __future__ import annotations

import pytest

from src.kb.agrifood_identity import AgrifoodIdentity, label_names
from src.kb.agrifood_records import AgrifoodError
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    yield item
    item.conn.close()


def _pair(crosswalks, a, b):
    return next(c for c in crosswalks if {(c["left"]["scheme"], c["left"]["code"]),
                                          (c["right"]["scheme"], c["right"]["code"])} == {a, b})


def test_label_names_are_exact_published_names():
    assert label_names("Maize (corn)") == ["Maize (corn)", "Maize", "corn"]
    assert label_names("Wheat") == ["Wheat"]


def test_equivalent_broader_narrower_and_rejected_mappings(env):
    identity = AgrifoodIdentity(env.conn, now=env.tick)
    published = identity.publish_codelists(NS, principal_id="curator", scopes=h.ALL)
    assert {"faostat-item", "nass-commodity", "psd-commodity"} <= set(published["codelists"])
    proposed = identity.propose(NS, principal_id="matcher", scopes=h.WRITE)["crosswalks"]
    assert {c["state"] for c in proposed} == {"proposed"}  # nothing accepted automatically
    fao_nass = _pair(proposed, ("faostat-item", "56"), ("nass-commodity", "CORN"))
    assert fao_nass["kind"] == "equivalent" and fao_nass["evidence"]["names"] == ["corn"]
    fao_psd = _pair(proposed, ("faostat-item", "56"), ("psd-commodity", "0440000"))
    fao_portal = _pair(proposed, ("faostat-item", "56"), ("agrifood-portal-product", "MAI"))
    # Before review, a query reaches only its own code.
    alone = identity.resolve_commodity(NS, "nass-commodity:CORN", scopes=h.READ)
    assert [(c["scheme"], c["code"]) for c in alone["codes"]] == [("nass-commodity", "CORN")]
    assert fao_nass["crosswalk_id"] in alone["pending_crosswalks"]

    # Equivalent: accepted, and the query reaches FAOSTAT with the mapping that connected it.
    accepted = identity.review(NS, fao_nass["crosswalk_id"], "accept", "same crop, grain", principal_id="rev",
                               scopes=h.REVIEW)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "rev"
    identity.review(NS, fao_psd["crosswalk_id"], "accept", "PSD corn is maize", principal_id="rev", scopes=h.REVIEW)
    reached = {(c["scheme"], c["code"]): c for c in identity.resolve_commodity(
        NS, "nass-commodity:CORN", scopes=h.READ)["codes"]}
    assert reached[("faostat-item", "56")]["match"] == "equivalent"
    assert reached[("faostat-item", "56")]["via"][0]["crosswalk_id"] == fao_nass["crosswalk_id"]
    assert reached[("psd-commodity", "0440000")]["match"] == "equivalent"  # transitively through FAOSTAT

    # Rejected: the portal's maize never joins, and a rejection can be reverted.
    rejected = identity.review(NS, fao_portal["crosswalk_id"], "reject", "portal price is a traded grade",
                               principal_id="rev", scopes=h.REVIEW)
    assert rejected["state"] == "rejected"
    assert ("agrifood-portal-product", "MAI") not in {(c["scheme"], c["code"]) for c in identity.resolve_commodity(
        NS, "faostat-item:56", scopes=h.READ)["codes"]}
    reverted = identity.revert(NS, fao_portal["crosswalk_id"], "reconsidered", principal_id="rev", scopes=h.REVIEW)
    assert reverted["state"] == "reverted" and [x["state"] for x in reverted["history"]] == [
        "proposed", "rejected", "reverted"]

    # Broader/narrower: an explicit reviewed mapping; shown beside, never aggregated or chained.
    manual = identity.propose_manual(NS, "faostat-item:15", "agrifood-portal-product:BLTPAN", "broader",
                                     "FAOSTAT wheat covers all wheat; milling wheat is one grade",
                                     principal_id="rev", scopes=h.REVIEW)
    identity.review(NS, manual["crosswalk_id"], "accept", "stated scope", principal_id="rev2", scopes=h.REVIEW)
    wheat = {(c["scheme"], c["code"]): c for c in identity.resolve_commodity(NS, "faostat-item:15",
                                                                             scopes=h.READ)["codes"]}
    assert wheat[("agrifood-portal-product", "BLTPAN")]["match"] == "narrower"
    assert "never aggregated" in wheat[("agrifood-portal-product", "BLTPAN")]["notice"]
    milling = {(c["scheme"], c["code"]): c for c in identity.resolve_commodity(
        NS, "agrifood-portal-product:BLTPAN", scopes=h.READ)["codes"]}
    assert milling[("faostat-item", "15")]["match"] == "broader"

    unmapped = identity.unmapped(NS, scopes=h.READ)
    assert {"scheme": "faostat-item", "code": "2514"} in [{"scheme": u["scheme"], "code": u["code"]}
                                                           for u in unmapped["unmapped"]]
    assert any(p["code"] == "BLTPAN" for p in unmapped["partial"])
    with pytest.raises(AgrifoodError):
        identity.propose_manual(NS, "faostat-item:15", "faostat-item:56", "broader", "x", principal_id="rev",
                                scopes=h.REVIEW)
    with pytest.raises(AgrifoodError):
        identity.review(NS, fao_nass["crosswalk_id"], "accept", "again", principal_id="rev", scopes=h.REVIEW)


def test_places_resolve_through_geospatial_by_source_codes(env):
    identity = AgrifoodIdentity(env.conn, now=env.tick)
    missing = identity.unmapped_places(NS, scopes=h.READ)["unmapped"]
    assert {"scheme": "us-fips", "code": "19169"} in missing
    identity.register_places(principal_id="curator", scopes=h.ALL)
    fao = identity.resolve_place(NS, "fao-area:231", scopes=h.ALL, principal_id="u")
    assert fao["status"] == "resolved" and {"scheme": "psd-country", "code": "US"} in fao["codes"]
    assert {"scheme": "iso3166-1", "code": "US"} in fao["codes"]
    saved = env.conn.execute("SELECT status, selected_place_id FROM geocode_resolutions WHERE resolution_id=?",
                             [fao["resolution_id"]]).fetchone()
    assert saved == ("resolved", fao["place_id"])
    county = identity.resolve_place(NS, "us-fips:19169", scopes=h.ALL)
    assert county["status"] == "resolved" and county["codes"] == [{"scheme": "us-fips", "code": "19169"}]
    by_name = identity.resolve_place(NS, "France", scopes=h.ALL)
    assert {"scheme": "eurostat-geo", "code": "FR"} in by_name["codes"]
    assert {"scheme": "fao-area", "code": "68"} in by_name["codes"]
    assert identity.resolve_place(NS, "Atlantis", scopes=h.ALL)["status"] == "unresolved"
    assert identity.unmapped_places(NS, scopes=h.READ)["unmapped"] == []
