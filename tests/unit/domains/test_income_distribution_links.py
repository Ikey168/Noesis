"""Income series linked to methodology, Demographics denominators and Labour indicators (#2583, IP07)."""

from __future__ import annotations

from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_links import IncomeLinks
from tests.unit import demographics_harness as dh
from tests.unit import income_distribution_harness as h
from tests.unit import labour_harness as lh


def test_missing_providers_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    links = IncomeLinks(conn)
    demographics = links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert demographics["linked"] == [] and demographics["unresolved"]
    assert labour["linked"] == [] and len(labour["unresolved"]) == 17
    reasons = {link["evidence"]["reason"].split(":")[0] for link in links.links(h.NS, scopes=h.READ_ONLY,
                                                                                 state="unresolved")}
    assert reasons == {"provider_absent"}
    references = links.link_references(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert references["linked"] == [] and references["unresolved"]  # documents not held stay citations


def test_links_record_their_basis_and_point_at_record_revisions():
    conn = h.connection()
    h.load_all(conn)
    lh.load_all(conn)
    dh.load_all(conn)
    places = h.register_places(conn, keys=("de",))
    lh.register_places(conn, keys=("de",))
    identity = IncomeIdentity(conn)
    for a in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo")["assertions"]:
        if a["state"] == "proposed":
            identity.review(h.NS, a["assertion_id"], "accept", "code", principal_id="reviewer", scopes=h.SCOPES)
    from src.kb.labour_identity import LabourIdentity

    labour_identity = LabourIdentity(conn)
    for a in labour_identity.propose_places(lh.NS, principal_id="analyst", scopes=lh.SCOPES,
                                            geo_namespace="geo")["assertions"]:
        if a["state"] == "proposed" and a["target"]["place_id"] == places["de"]:
            labour_identity.review(lh.NS, a["assertion_id"], "accept", "code", principal_id="reviewer",
                                   scopes=lh.SCOPES)
    links = IncomeLinks(conn)
    demographics = links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    (denominator,) = [links.link(h.NS, link_id) for link_id in demographics["linked"]]
    assert denominator["basis"] == "shared_identifier" and denominator["target"]["id"].startswith("dm-series:")
    assert denominator["target"]["vintage_id"].startswith("dm-vintage:")
    assert denominator["income_vintage_id"].startswith("inc-vintage:")
    assert denominator["evidence"]["overlapping_periods"]
    assert demographics["missing"]  # Austria's named denominator is not held: reported
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    made = [links.link(h.NS, link_id) for link_id in labour["linked"]]
    bases = {link["basis"] for link in made}
    assert bases == {"shared_identifier", "accepted_match"}
    matched = next(link for link in made if link["basis"] == "accepted_match")
    assert matched["evidence"]["via"]["place_id"] == places["de"]
    assert all(link["target"]["vintage_id"].startswith("lb-vintage:") and link["evidence"]["overlapping_periods"]
               for link in made)
    assert labour["missing"]  # series without a labour counterpart (Indonesia, the SSF region) are reported
    # Idempotent: re-linking adds nothing.
    assert links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)["linked"] == []
