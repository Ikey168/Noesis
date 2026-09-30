"""Offline protein-to-reference-records acceptance for the Science life-sciences features (#2652, LS13 #2716).

The journey replays the pinned UniProt (reviewed, unreviewed and merged entries,
UniSave history), NCBI (live and replaced genes, a taxon and a merged taxon),
RCSB PDB (two entries, one superseding an obsolete entry) and ChEMBL (one
release: target, compounds, activities and cited documents) fixtures through the
source-pack runtime with sockets blocked, seeds fictional Chemicals,
Biodiversity, Clinical medicines and literature records, and drives the MCP
tools: identity review, cross-pack links, an entry as of a release with its
cross-reference graph, published activity against the target, a subject with
no records, the next releases heard by a monitor and a cited evidence bundle.
Identifiers and values are fictional, not live evidence.
"""

from __future__ import annotations

import asyncio
import json
import socket

import duckdb
import pytest

from src.kb.lifesci_records import forbidden_keys
from tests.unit import lifesci_harness as h
from tools.knowledge_engine_mcp import server


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _tools(monkeypatch, path):
    state = {"principal": "alice", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def _pair(matches, a, b):
    return next(m for m in matches if {m["left_key"], m["right_key"]} == {a, b})


def test_protein_to_cited_reference_records_structures_and_published_bioactivity(tmp_path, monkeypatch):
    path = str(tmp_path / "lifesci.duckdb")
    env = h.Env(duckdb.connect(path)).loaded()
    assert {r["evidence_origin"] for r in env.store.runs(h.NS)} == {"fixture"}
    substance = env.seed_substance()
    env.seed_biodiversity_taxon()
    medicine = env.seed_medicine()
    paper = env.seed_paper()
    env.conn.close()
    tools, state = _tools(monkeypatch, path)

    # Reviewable identity: published cross-references first, InChIKey and Tax ID proposals, nothing accepted.
    proposed = tools["propose_lifesci_identity_matches"].fn(namespace=h.NS)
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    assert "uniprot:A0AZZ1ZZZ2" in proposed["unmatched"]
    compound = _pair(proposed["matches"], "chembl-compound:CHEMBL9990101", f"chemicals:{substance}")
    taxon = _pair(proposed["matches"], "ncbitaxon:999001", "biodiversity:gbif:9990001")
    structure = _pair(proposed["matches"], "uniprot:P0DZZ1", "pdb:9ZZ2")
    state["principal"] = "bob"
    for match in (compound, taxon, structure):
        reviewed = tools["review_lifesci_identity_match"].fn(namespace=h.NS, match_id=match["match_id"],
                                                             decision="accept", reason="published identifiers")
        assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    gene = _pair(proposed["matches"], "uniprot:P0DZZ1", "ncbigene:99990001")
    rejected = tools["review_lifesci_identity_match"].fn(namespace=h.NS, match_id=gene["match_id"],
                                                         decision="reject", reason="fixture reviewer disagrees")
    reverted = tools["revert_lifesci_identity_match"].fn(namespace=h.NS, match_id=rejected["match_id"],
                                                         reason="re-open")
    assert reverted["state"] == "reverted"

    # Cross-pack links: accepted matches, shared identifiers and citations, each to a record revision.
    linked = tools["link_lifesci_records"].fn(namespace=h.NS)
    assert linked["created"] == {"chemicals": 1, "biodiversity": 1, "medicines": 2} and linked["missing"] == []
    kinds = {(link["target_kind"], link["target_id"]) for link in linked["links"]}
    assert ("chemicals-substance", substance) in kinds and ("clinical-medicine", medicine) in kinds
    assert ("publication", paper) in kinds
    assert all(link["revision_id"] for link in linked["links"])

    # A protein as of a release: version, successors of the merged accession, labelled cross-references.
    entry = tools["lifesci_entry_as_of"].fn(namespace=h.NS, identifier="Q9ZZZ1", release="2099_01")
    assert entry["resolution"][0]["to"] == ["uniprot:P0DZZ1"]
    protein = entry["entries"][1]
    assert (protein["as_published"]["entry_version"], protein["as_published"]["sequence_version"]) == (20, 2)
    outgoing = {o["subject_key"]: o for o in protein["cross_references"]["outgoing"] if o["subject_key"]}
    assert outgoing["pdb:9ZZ2"]["identity"]["state"] == "accepted" and outgoing["pdb:9ZZ3"]["identity"]["state"] \
        == "proposed"
    assert outgoing["ncbigene:99990001"]["identity"]["state"] == "reverted"
    assert {link["target_kind"] for link in protein["links"]} >= {"publication"}
    assert len(entry["citations"]) == 2 and all(c["revision_id"] and c["retrieved_on"] for c in entry["citations"])
    structures = tools["lifesci_entry_as_of"].fn(namespace=h.NS, identifier="9ZZ1")
    assert structures["resolution"][0]["to"] == ["pdb:9ZZ3"]
    assert structures["entries"][1]["as_published"]["experimental_methods"] == ["ELECTRON MICROSCOPY"]

    # Published bioactivity against the protein's ChEMBL target, as published, never aggregated.
    activities = tools["lifesci_target_activities"].fn(namespace=h.NS, target="P0DZZ1")
    assert activities["release"] == "ChEMBL_99" and activities["aggregation"] == "none"
    assert {r["activity_id"] for r in activities["activities"]} == {"99990001", "99990002", "99990003"}
    assert all(r["citation"]["revision_id"] and r["document"]["document_chembl_id"] for r in activities["activities"])

    # A subject with no records is explicit.
    none = tools["lifesci_entry_as_of"].fn(namespace=h.NS, identifier="P0DZZ9")
    assert none["status"] == "not_on_record" and none["unknowns"][0]["kind"] == "not_on_record"
    empty = tools["lifesci_target_activities"].fn(namespace=h.NS, target="CHEMBL9990999")
    assert empty["status"] == "not_on_record"

    # The next releases reach a monitor once, citing prior and new revisions.
    monitor = tools["create_lifesci_monitor"].fn(namespace=h.NS, request_key="kinase", accessions=["P0DZZ1"],
                                                 targets=["CHEMBL9990201"])
    assert tools["run_lifesci_monitor"].fn(subscription_id=monitor["subscription_id"])["baseline"]
    later = h.Env(duckdb.connect(path), clock=env.clock)
    later.later()
    later.conn.close()
    heard = tools["run_lifesci_monitor"].fn(subscription_id=monitor["subscription_id"])
    notified = {(n["kind"], n["record_key"]) for n in heard["notifications"]}
    assert {("entry_revised", "P0DZZ1"), ("activity_added", "99990004"), ("activity_removed", "99990002")} <= notified
    assert all(n["new"]["revision_id"] for n in heard["notifications"])
    assert tools["run_lifesci_monitor"].fn(subscription_id=monitor["subscription_id"])["notifications"] == []
    latest = tools["lifesci_target_activities"].fn(namespace=h.NS, target="CHEMBL9990201")
    assert latest["release"] == "ChEMBL_100" and latest["removed_in_release"][0]["activity_id"] == "99990002"
    revision_history = tools["lifesci_entry_as_of"].fn(namespace=h.NS, identifier="P0DZZ1")["entries"][0]["history"]
    assert [r["entry_version"] for r in revision_history] == [20, 21]

    # Evidence bundle cites every item with source, revision and as-of time.
    bundle = tools["export_lifesci_evidence_bundle"].fn(namespace=h.NS, identifier="P0DZZ1", release="2099_01")
    evidence = [o["payload"] for o in bundle["bundle"]["objects"]
                if o["payload"].get("kind") == "lifesci-record-revision"]
    assert evidence and all(e["source"] and e["locator"]["revision_id"] and e["as_of"]["retrieved_at_ms"]
                            for e in evidence)

    # Exclusions and the minimisation decision hold on every answer.
    for answer in (entry, structures, activities, latest, heard, linked, bundle):
        assert forbidden_keys(json.loads(json.dumps(answer))) == []
        assert "Placeholder" not in json.dumps(answer)
    for answer in (entry, activities, bundle):
        assert "activity prediction" in answer["exclusions"] and answer["minimisation"] == "no personal data stored"
