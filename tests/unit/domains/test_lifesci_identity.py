"""Reviewable identity across the life-science sources, Chemicals and Biodiversity (#2652, LS07 #2686)."""

from __future__ import annotations

import pytest

from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_records import LifeSciError
from tests.unit import lifesci_harness as h


def pair(result, a, b):
    return next(m for m in result["matches"] if {m["left_key"], m["right_key"]} == {a, b})


def test_published_cross_references_come_first_and_nothing_is_accepted_or_merged():
    env = h.Env().loaded()
    result = LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    assert {m["state"] for m in result["matches"]} == {"proposed"}
    for other in ("pdb:9ZZ2", "pdb:9ZZ3", "ncbigene:99990001", "chembl-target:CHEMBL9990201"):
        match = pair(result, "uniprot:P0DZZ1", other)
        assert match["method"] == "published-cross-reference" and match["confidence"] == "high"
        assert {a["source"] for a in match["evidence"]["asserted_by"]} >= {"uniprot"}
    assert "uniprot:A0AZZ1ZZZ2" in result["unmatched"]
    assert "chembl-compound:CHEMBL9990101" in result["unmatched"]  # Chemicals not installed: unmatched, visible
    assert {s["target"] for s in result["skipped_targets"]} == {"chemicals.substances", "environment.biodiversity"}
    assert "uniprot:Q9ZZZ1" not in {k for m in result["matches"] for k in (m["left_key"], m["right_key"])}
    again = LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    assert again["proposed"] == []


def test_compounds_match_chemicals_by_inchikey_and_taxa_match_biodiversity_by_published_tax_id():
    env = h.Env().loaded()
    env.seed_substance()
    env.seed_biodiversity_taxon()
    result = LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    compound = pair(result, "chembl-compound:CHEMBL9990101", "chemicals:pubchem:cid:999001")
    assert compound["method"] == "inchikey" and compound["confidence"] == "medium"
    assert compound["evidence"]["inchikey"] == "ZZZZZZZZZZZZZA-UHFFFAOYSA-N"
    taxon = pair(result, "ncbitaxon:999001", "biodiversity:gbif:9990001")
    assert taxon["method"] == "taxon-cross-reference" and taxon["evidence_class"] == "published-identifier"
    assert "chembl-compound:CHEMBL9990102" in result["unmatched"]


def test_names_only_propose_low_confidence_taxon_candidates_when_no_identifier_connects():
    env = h.Env().loaded()
    env.seed_biodiversity_taxon(cross_reference=False)
    result = LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    taxon = pair(result, "ncbitaxon:999001", "biodiversity:gbif:9990001")
    assert (taxon["method"], taxon["confidence"], taxon["evidence_class"]) == ("scientific-name", "low",
                                                                              "lower-evidence")


def test_review_accept_reject_and_revert_are_entity_history_decisions():
    env = h.Env().loaded()
    identity = LifeSciIdentity(env.conn)
    result = identity.propose(h.NS, principal_id="alice", scopes=h.ALL)
    match = pair(result, "uniprot:P0DZZ1", "pdb:9ZZ2")
    with pytest.raises(LifeSciError):
        identity.review(h.NS, match["match_id"], "accept", "ok", principal_id="bob", scopes=h.WRITE)
    with pytest.raises(LifeSciError):
        identity.review(h.NS, match["match_id"], "accept", "  ", principal_id="bob", scopes=h.ALL)
    accepted = identity.review(h.NS, match["match_id"], "accept", "UniProt and PDB both assert it",
                               principal_id="bob", scopes=h.ALL)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    assert identity.accepted_for(h.NS, "pdb:9ZZ2")[0]["match_id"] == match["match_id"]
    reverted = identity.revert(h.NS, match["match_id"], "re-check", principal_id="carol", scopes=h.ALL)
    assert reverted["state"] == "reverted" and identity.accepted(h.NS) == []
    rejected = identity.review(h.NS, match["match_id"], "reject", "different construct", principal_id="bob",
                               scopes=h.ALL)
    assert rejected["state"] == "rejected"
    assert [h_["state"] for h_ in rejected["history"]] == ["proposed", "accepted", "reverted", "rejected"]
    decisions = env.conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0]
    assert decisions >= 3
