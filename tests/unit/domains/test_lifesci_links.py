"""Life-science records linked to other packs by citation, shared identifier and accepted match (#2652, LS08 #2691)."""

from __future__ import annotations

from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_links import LifeSciLinks
from tests.unit import lifesci_harness as h


def accept_all(env):
    identity = LifeSciIdentity(env.conn)
    for match in identity.propose(h.NS, principal_id="alice", scopes=h.ALL)["matches"]:
        identity.review(h.NS, match["match_id"], "accept", "published identifiers agree", principal_id="bob",
                        scopes=h.ALL)


def test_links_record_their_basis_and_point_at_record_revisions():
    env = h.Env().loaded()
    env.seed_substance()
    env.seed_biodiversity_taxon()
    medicine = env.seed_medicine()
    paper = env.seed_paper()
    accept_all(env)
    result = LifeSciLinks(env.conn).link(h.NS, principal_id="alice", scopes=h.ALL)
    assert result["created"] == {"chemicals": 1, "biodiversity": 1, "medicines": 2}
    assert result["missing"] == []
    by_kind = {}
    for link in result["links"]:
        by_kind.setdefault(link["target_kind"], []).append(link)
        assert link["revision_id"].startswith("lifesci-revision:") and link["basis"] in {
            "accepted-match", "shared-identifier", "citation"}
    (substance,) = by_kind["chemicals-substance"]
    assert substance["basis"] == "accepted-match" and substance["target_revision"]
    assert substance["detail"]["reviewer"] == "bob"
    (occurrence,) = by_kind["biodiversity-occurrence"]
    assert occurrence["detail"]["gbif_id"] == "9990000001" and occurrence["target_revision"]
    assert {m["target_id"] for m in by_kind["clinical-medicine"]} == {medicine}
    assert all(m["basis"] == "shared-identifier" for m in by_kind["clinical-medicine"])
    (publication,) = by_kind["publication"]
    assert publication["target_id"] == paper and publication["basis"] == "citation"
    assert len(by_kind["chembl-document"]) == 3  # every activity cites its document
    assert {u["detail"]["doi"] for u in by_kind["unresolved-citation"]} >= {"10.5555/fict.lifesci.2099.3"}
    again = LifeSciLinks(env.conn).link(h.NS, principal_id="alice", scopes=h.ALL)
    assert len(again["links"]) == len(result["links"])


def test_missing_packs_and_unreviewed_matches_are_reported_not_linked():
    env = h.Env().loaded()
    LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)  # proposals only
    result = LifeSciLinks(env.conn).link(h.NS, principal_id="alice", scopes=h.ALL)
    missing = {m["target"] for m in result["missing"]}
    assert {"chemicals.substances", "environment.biodiversity", "clinical.medicines", "science.literature"} <= missing
    assert result["created"] == {"chemicals": 0, "biodiversity": 0, "medicines": 0}
    assert {link["target_kind"] for link in result["links"]} <= {"chembl-document", "unresolved-citation"}


def test_a_proposed_inchikey_match_never_carries_a_link_and_a_caller_without_clinical_read_is_told():
    env = h.Env().loaded()
    env.seed_substance()
    env.seed_medicine()
    LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    scopes = h.WRITE | {"knowledge:substances:read"}
    result = LifeSciLinks(env.conn).link(h.NS, principal_id="alice", scopes=scopes)
    assert result["created"]["chemicals"] == 0 and result["created"]["medicines"] == 0
    assert any(m["target"] == "clinical.medicines" and "knowledge:clinical:read" in m["reason"]
               for m in result["missing"])
