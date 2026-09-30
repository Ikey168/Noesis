"""Treaty records linked to Legal works, sanctions legal bases and trade reporters (#2615)."""

from __future__ import annotations

import pytest

from src.kb.treaties_identity import TreatiesIdentity
from src.kb.treaties_links import TreatiesLinks
from tests.unit import treaties_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def test_absent_providers_are_reported_not_dropped(conn):
    result = TreatiesLinks(conn, now=h.Clock()).link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["links"] == []
    assert {p["pack"] for p in result["provider_unavailable"]} == {"legal.core", "legal.sanctions", "economics.trade"}


def test_links_record_their_basis_and_point_at_revisions(conn):
    clock = h.Clock()
    work_id = h.seed_legal_act(conn)
    h.seed_sanctions_bases(conn)
    places = h.seed_places(conn)
    trade_assertion = h.seed_trade_area(conn, places["DE"])
    identity = TreatiesIdentity(conn, now=clock)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    germany = next(c for c in proposed["candidates"] if c["subject"] == "treaties:participant:coe:germany")
    identity.review(h.NS, germany["candidate_id"], "accept", "coded place", principal_id="bob",
                    scopes=h.REVIEW_SCOPES)
    links = TreatiesLinks(conn, now=clock)
    result = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["provider_unavailable"] == []
    by_kind: dict[str, list] = {}
    for link in result["links"]:
        by_kind.setdefault(link["link_kind"], []).append(link)
    (act,) = [x for x in by_kind["eu-act"] if x["status"] == "linked"]
    assert act["target_key"] == work_id and act["basis"] == "citation"
    assert act["treaty_revision_id"].startswith("treaty-revision:") and act["treaty_key"] == h.CELLAR_TREATY
    assert act["evidence"]["cellar_relation"].endswith("#work_cites_work")
    missing = [x for x in by_kind["eu-act"] if x["status"] == "missing_target"]
    assert [x["target_key"] for x in missing] == ["celex:32098D0901"]
    sanctions = {x["status"]: x for x in by_kind["sanctions-legal-basis"]}
    assert sanctions["linked"]["treaty_key"] == h.COE_TREATY
    assert sanctions["linked"]["target_revision"] == "sanctions-snapshot:fixture-1"
    assert sanctions["missing_target"]["evidence"]["cited"]["as_written"] == "CETS No. 991"
    (trade,) = by_kind["participant-trade-reporter"]
    assert trade["basis"] == "accepted-match" and trade["target_revision"] == trade_assertion
    assert trade["subject_key"] == "treaties:participant:coe:germany" and "nothing says" in trade["evidence"]["note"]
    again = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert {x["link_id"] for x in again["links"]} == {x["link_id"] for x in result["links"]}
    assert len(links.links(h.NS, scopes=h.READ_ONLY)) == len(result["links"])


def test_a_new_treaty_revision_is_linked_afresh_and_the_old_link_stays(conn):
    h.seed_legal_act(conn)
    links = TreatiesLinks(conn, now=h.Clock())
    first = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    h.apply(conn, h.CELLAR, v2=True)
    second = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    old = {x["treaty_revision_id"] for x in first["links"]}
    new = {x["treaty_revision_id"] for x in second["links"]}
    assert old.isdisjoint(new)
    stored = links.links(h.NS, scopes=h.READ_ONLY, treaty_key=h.CELLAR_TREATY)
    assert {x["treaty_revision_id"] for x in stored} == old | new
    assert "celex:32100R0007" in {x["target_key"] for x in second["missing_targets"]}
