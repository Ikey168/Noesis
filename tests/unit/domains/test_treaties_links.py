"""Treaty records linked to Legislation, Sanctions and Trade flows by citation and accepted matches (#2615, TR07)."""

from __future__ import annotations

import pytest

from src.kb.treaties_identity import TreatiesIdentity, place_key
from src.kb.treaties_links import TreatiesLinks
from src.kb.treaties_records import TreatiesStore, forbidden_keys
from tests.unit import treaties_harness as h


@pytest.fixture
def world():
    conn = h.connection()
    h.load_all(conn)
    yield conn
    conn.close()


def test_missing_providers_and_targets_are_reported_not_dropped(world):
    result = TreatiesLinks(world).link_all(h.NS, principal_id="analyst", scopes=h.SCOPES)
    reported = {u["provider"] for u in result["unavailable"]}
    assert reported == {"legal.core", "legal.sanctions", "economics.trade"}
    legal = [link for link in result["links"] if link["target_kind"] == "legal-work"]
    assert {link["target_key"] for link in legal} == {"celex:22090A0510(01)", "celex:32092D0101",
                                                      "celex:32093D0202", "celex:32094R0303"}
    assert {link["status"] for link in legal} == {"provider-missing"}


def test_links_record_their_basis_and_point_at_record_revisions(world):
    work_id = h.seed_legal_work(world, "32093D0202")
    h.seed_sanctions_basis(world, "Council Decision implementing Convention XXVII-99 (UNTC) and CETS No. 999")
    h.seed_sanctions_basis(world, "Regulation (EU) 2099/1 - unrelated measure", celex="32099R0001")
    h.seed_trade_reporter(world, "XEA")
    places = h.seed_places(world)
    links = TreatiesLinks(world)
    result = links.link_all(h.NS, principal_id="analyst", scopes=h.SCOPES)
    by = {(link["treaty_key"], link["target_kind"], link["target_key"]): link for link in result["links"]}
    concluded = by[(h.EU, "legal-work", work_id)]
    assert concluded["status"] == "resolved" and concluded["basis"] == "citation"
    assert concluded["evidence"]["citation"]["relation_as_published"].endswith("resource_legal_based_on_resource_legal")
    treaty_revision = TreatiesStore(world).records(h.NS, scopes=h.READ_ONLY, record_keys=[h.EU])[0]["revision_id"]
    assert concluded["treaty_revision_id"] == treaty_revision
    assert by[(h.EU, "legal-work", "celex:32092D0101")]["status"] == "target-missing"
    own = by[(h.EU, "legal-work", "celex:22090A0510(01)")]
    assert own["basis"] == "shared-identifier" and own["status"] == "target-missing"
    sanctions = [link for link in result["links"] if link["target_kind"] == "sanctions-legal-basis"]
    assert {(link["treaty_key"], link["evidence"]["identifier"]) for link in sanctions} == {
        (h.UNTC, "XXVII-99"), (h.COE, "CETS No. 999"), (h.EU, "CETS No. 999")}
    assert all(link["basis"] == "citation" and link["target_revision"] == "snapshot:fixture" for link in sanctions)
    trade = [link for link in result["links"] if link["target_kind"] == "trade-reporter"]
    assert [(link["subject_key"], link["basis"]) for link in trade] == [
        ("treaties:eu-cellar:participant:xea", "shared-identifier")]
    assert trade[0]["target_key"] == "trade-reporter:m49:999" and trade[0]["evidence"]["iso3"] == "XEA"
    # an accepted TR06 match adds the accepted-match route for a participant without a published code
    identity = TreatiesIdentity(world)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace=h.NS)
    named = next(c for c in proposed["candidates"]
                 if set(c["records"]) == {"treaties:untc:participant:exampland", place_key(places["XEA"])})
    identity.review(h.NS, named["candidate_id"], "accept", "reviewed", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    again = links.link_all(h.NS, principal_id="analyst", scopes=h.SCOPES)
    matched = [link for link in again["links"] if link["subject_key"] == "treaties:untc:participant:exampland"]
    assert [(link["basis"], link["evidence"]["candidate_id"]) for link in matched] == [
        ("accepted-match", named["candidate_id"])]
    assert len(links.links(h.NS, scopes=h.READ_ONLY)) == len({link["link_id"] for link in again["links"]})
    assert not forbidden_keys(again)
