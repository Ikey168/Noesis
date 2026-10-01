"""Reviewable cross-source identity for life-science records (#2652, LS07 #2686)."""

from __future__ import annotations

import pytest

from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_records import LifeSciError, record_id_for
from tests.unit import lifesci_harness as h

EXA1 = record_id_for(h.NS, "uniprot", "protein", "X9EXA1")
NRW1 = record_id_for(h.NS, "uniprot", "protein", "X9NRW1")
PDB_A = record_id_for(h.NS, "rcsb-pdb", "structure", "9EXA")
TARGET = record_id_for(h.NS, "chembl", "target", "CHEMBL9900001")
COMPOUND = record_id_for(h.NS, "chembl", "compound", "CHEMBL9900101")
TAXON = record_id_for(h.NS, "ncbi-taxonomy", "taxon", "99000001")
NORTH = record_id_for(h.NS, "ncbi-taxonomy", "taxon", "99000100")


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    h.seed_all(conn)
    identity = LifeSciIdentity(conn, now=lambda: h.SECOND + 1)
    proposed = identity.propose(h.NS, scopes=h.SCOPES, principal_id="matcher")
    return identity, proposed


def pair(proposed, left, right):
    return next(m for m in proposed["matches"] if {m["left_id"], m["right_id"]} == {left, right})


def test_published_cross_references_are_proposed_first_and_nothing_is_accepted(env):
    _, proposed = env
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    structure = pair(proposed, EXA1, PDB_A)
    assert structure["method"] == "published-xref" and structure["confidence"] == 0.95
    assert {e["asserted_by"] for e in structure["evidence"]} == {"uniprot", "rcsb-pdb"}
    assert all(e["revision_id"] for e in structure["evidence"])
    target = pair(proposed, EXA1, TARGET)
    assert {e["asserted_by"] for e in target["evidence"]} == {"uniprot", "chembl"}
    gene = pair(proposed, EXA1, record_id_for(h.NS, "ncbi-gene", "gene", "990000101"))
    assert gene["confidence"] == 0.9 and [e["asserted_by"] for e in gene["evidence"]] == ["uniprot"]
    # A merged accession asserts nothing any more.
    assert not [m for m in proposed["matches"]
                if record_id_for(h.NS, "uniprot", "protein", "X9EXA2") in {m["left_id"], m["right_id"]}]


def test_compounds_meet_substances_by_inchikey_and_taxa_by_published_tax_id_before_names(env):
    _, proposed = env
    substance = next(m for m in proposed["matches"] if m["right_kind"] == "substance")
    assert (substance["left_id"], substance["right_id"], substance["method"]) == (COMPOUND, "pubchem:cid:99000101",
                                                                                 "inchikey")
    assert substance["evidence"][0]["value"] == h.EXAMPLINIB_KEY
    taxa = {m["left_id"]: m for m in proposed["matches"] if m["right_kind"] == "biodiversity-taxon"}
    assert (taxa[TAXON]["method"], taxa[TAXON]["right_id"]) == ("published-xref", "gbif:9900001")
    assert (taxa[NORTH]["method"], taxa[NORTH]["confidence"]) == ("scientific-name", 0.3)


def test_review_accept_reject_and_revert_are_entity_history_decisions(env):
    identity, proposed = env
    match = pair(proposed, EXA1, PDB_A)
    accepted = identity.review(h.NS, match["match_id"], "accepted", "PDB and UniProt assert each other",
                               scopes=h.SCOPES, principal_id="rev")
    assert accepted["state"] == "accepted" and accepted["decision_id"].startswith("entity-decision:")
    with pytest.raises(LifeSciError) as exc:
        identity.review(h.NS, match["match_id"], "rejected", "again", scopes=h.SCOPES, principal_id="rev")
    assert exc.value.code == "invalid_state"
    reverted = identity.revert(h.NS, match["match_id"], "re-check", scopes=h.SCOPES, principal_id="rev")
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == ["accepted", "reverted"]
    name = next(m for m in proposed["matches"] if m["method"] == "scientific-name")
    rejected = identity.review(h.NS, name["match_id"], "rejected", "names alone do not identify a taxon",
                               scopes=h.SCOPES, principal_id="rev")
    assert rejected["state"] == "rejected"
    # A re-proposal never overrides a review.
    again = identity.propose(h.NS, scopes=h.SCOPES, principal_id="matcher")
    assert next(m for m in again["matches"] if m["match_id"] == name["match_id"])["state"] == "rejected"
    with pytest.raises(LifeSciError) as exc:
        identity.review(h.NS, name["match_id"], "accepted", "x", scopes=h.READ_ONLY, principal_id="rev")
    assert exc.value.code == "unauthorized"


def test_unmatched_records_stay_visible_and_manual_proposals_carry_evidence(env):
    identity, proposed = env
    unmatched = identity.unmatched(h.NS, scopes=h.READ_ONLY, record_type="protein")
    assert {r["native_id"] for r in unmatched["unmatched"]} == {"X9EXA1", "X9EXA2", "X9NRW1"}
    identity.review(h.NS, pair(proposed, EXA1, PDB_A)["match_id"], "accepted", "mutual", scopes=h.SCOPES,
                    principal_id="rev")
    unmatched = identity.unmatched(h.NS, scopes=h.READ_ONLY, record_type="protein")
    assert {r["native_id"] for r in unmatched["unmatched"]} == {"X9EXA2", "X9NRW1"}
    manual = identity.propose_manual(h.NS, NRW1, "lifesci-record", NORTH, "curator note: organism of X9NRW1",
                                     scopes=h.SCOPES, principal_id="curator")
    assert manual["method"] == "reviewed-manual" and manual["state"] == "proposed"
    with pytest.raises(LifeSciError):
        identity.propose_manual(h.NS, NRW1, "lifesci-record", NORTH, " ", scopes=h.SCOPES, principal_id="c")


def test_unresolved_cross_references_are_reported_not_matched():
    conn = h.connection()
    h.apply(conn, "uniprot", at_ms=h.FIRST)
    proposed = LifeSciIdentity(conn).propose(h.NS, scopes=h.SCOPES, principal_id="matcher")
    assert proposed["matches"] == []
    assert {(u["database"], u["id"]) for u in proposed["unresolved_xrefs"]} >= {("PDB", "9EXA"),
                                                                                  ("ChEMBL", "CHEMBL9900001")}
