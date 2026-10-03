"""SS07 (#2779): social protection series linked to Demographics and Public finance by citation or accepted match."""

from __future__ import annotations

from src.kb.social_protection_links import SocialProtectionLinks
from src.kb.social_protection_records import forbidden_paths
from tests.unit import demographics_harness as dh
from tests.unit import social_protection_harness as h


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    links = SocialProtectionLinks(conn)
    demographics = links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    cofog = links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert demographics["missing"] and cofog["missing"] and not demographics["linked"] and not cofog["linked"]
    states = {(link["kind"], link["state"]) for link in links.links(h.NS, scopes=h.READ_ONLY)}
    assert states == {("denominator", "provider_absent"), ("cofog", "provider_absent")}
    absent = links.links(h.NS, scopes=h.READ_ONLY, kind="cofog")[0]
    assert "economics.public-finance is not composed" in absent["evidence"]["reason"] and absent["vintage_id"]


def test_shared_codes_and_accepted_matches_link_pinned_revisions_without_derived_values():
    conn = h.connection()
    h.load_all(conn)
    dh.apply(conn, "eurostat", 0)  # Eurostat demo_pjan for DE (authored demographics fixture)
    h.load_public_finance(conn)  # COFOG GF10 for DE (authored)
    links = SocialProtectionLinks(conn)
    links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    total = h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.MIO_EUR.DE")
    (denominator,) = [k for k in links.links(h.NS, scopes=h.READ_ONLY, series_id=total["series_id"],
                                             kind="denominator") if k["state"] == "linked"]
    assert denominator["basis"] == "shared-identifier" and denominator["target"]["series_code"] == "demo_pjan"
    assert denominator["vintage_id"] == total["current_vintage_id"] and denominator["target"]["vintage_id"]
    (cofog,) = [k for k in links.links(h.NS, scopes=h.READ_ONLY, series_id=total["series_id"], kind="cofog")
                if k["state"] == "linked"]
    assert cofog["provider_id"] == "economics.public-finance" and cofog["target"]["cofog"]["code"] == "GF10"
    assert cofog["target"]["vintage_id"] == "gov_10a_exp@4049038800000"
    assert cofog["evidence"]["shared_reference_years"] == ["2094", "2095", "2096"]
    assert "distinct" in cofog["evidence"]["concept_note"]
    # No ratio, share or per-capita figure is computed from linked series.
    for link in links.links(h.NS, scopes=h.READ_ONLY):
        assert forbidden_paths(link) == [] and not {"value", "values", "observations"} & set(link["target"] or {})
    # France has no held targets: reported, never dropped.
    fr = h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.MIO_EUR.FR")
    assert {k["state"] for k in links.links(h.NS, scopes=h.READ_ONLY, series_id=fr["series_id"])} == {
        "target_not_held"}
    # SOCX states DEU: no target until an accepted place match supplies the DE code.
    socx = h.series(conn, "oecd-socx", "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL")
    assert {k["state"] for k in links.links(h.NS, scopes=h.READ_ONLY, series_id=socx["series_id"])} == {
        "target_not_held"}
    h.accept_places(conn, keys=("de",))
    links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    via_place = [k for k in links.links(h.NS, scopes=h.READ_ONLY, series_id=socx["series_id"], kind="cofog")
                 if k["state"] == "linked"]
    assert via_place and via_place[0]["basis"] == "accepted-match" and via_place[0]["evidence"]["assertion_id"]
    assert via_place[0]["reference"] == {"scheme": "eurostat-geo", "code": "DE"}
    # Idempotent.
    assert links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES) == {"linked": [], "missing": []}
