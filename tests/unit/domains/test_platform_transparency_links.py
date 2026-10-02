"""Ads linked to elections, campaign finance, lobbying and ownership by declared selection, published identifier or
accepted match (#2621)."""

from __future__ import annotations

from src.kb.platform_transparency_identity import PlatformTransparencyIdentity
from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_records import PlatformTransparencyStore
from tests.unit import platform_transparency_harness as h


def test_links_record_their_basis_and_point_at_specific_ad_revisions():
    conn = h.accepted_world()
    links = PlatformTransparencyLinks(conn)
    result = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert result["unavailable"] == []
    assert {k: v["status"] for k, v in result["kinds"].items()} == dict.fromkeys(
        ("election", "campaign-finance", "lobbying", "ownership"), "linked")
    store = PlatformTransparencyStore(conn)
    for link in links.links(h.NS, scopes=h.SCOPES):
        assert store.revision(h.NS, link["record_revision_id"], scopes=h.SCOPES)["record_key"] == link["record_key"]
        assert link["basis"] and link["target_revision"]
    cf = links.links(h.NS, scopes=h.SCOPES, kind="campaign-finance",
                     target_key="campaign-finance:fec:committee:C00999901")
    assert len(cf) == 3 and {lk["basis"]["method"] for lk in cf} == {"published-id"}
    assert cf[0]["basis"]["filings_on_record"]  # the matched committee's filing versions, by revision
    election = links.links(h.NS, scopes=h.SCOPES, kind="election", target_key=h.UK_ELECTION)
    assert {lk["basis"]["basis"] for lk in election} == {"declared-selection", "published-election-label"}
    assert all(lk["basis"]["election_date"] == "2099-05-07" and "ad_period" in lk["basis"] for lk in election)
    lobbying = links.links(h.NS, scopes=h.SCOPES, kind="lobbying")
    assert [lk["target_key"] for lk in lobbying] == ["lobbying:us-lda:9002-8002"]
    assert lobbying[0]["basis"]["register_revision_id"] == lobbying[0]["target_revision"]
    ownership = links.links(h.NS, scopes=h.SCOPES, kind="ownership")
    assert {lk["target_key"] for lk in ownership} == {"gb-coh:09990002"}
    funding = "platform-transparency:meta:funding-entity:example-holdings-ltd"
    assert {lk["subject_key"] for lk in ownership} == {"platform-transparency:meta:advertiser:999000001", funding}
    again = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert all(v["linked"] == 0 for v in again["kinds"].values())  # idempotent
    assert all("coordination" in lk["notice"] for lk in links.links(h.NS, scopes=h.SCOPES))


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    PlatformTransparencyIdentity(conn).propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    result = PlatformTransparencyLinks(conn).link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["unavailable"] == ["campaign-finance", "election", "lobbying", "ownership"]
    assert result["kinds"]["election"]["reason"].startswith("the Political elections")
    world = h.connection()
    h.load_all(world)
    from tests.unit import elections_harness as eh

    eh.apply(world, "gb", eh.UK)  # only the UK election is on record
    found = PlatformTransparencyLinks(world).link_elections(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert found["status"] == "linked"
    assert {m["target_key"] for m in found["missing_targets"]} == {h.US_ELECTION}
    assert all(m["reason"] for m in found["missing_targets"])
