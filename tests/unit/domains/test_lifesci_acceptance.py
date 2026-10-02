"""Offline protein-to-reference-records acceptance for the Science life-sciences provider (LS13, #2716).

The pinned synthetic UniProt, NCBI Gene, NCBI Taxonomy, RCSB PDB and ChEMBL fixtures (authored in the documented
shapes; every accession, organism, compound and title is fictional, and the author fields they carry exist only to
prove they are dropped) replay through the real ``life-sciences`` adapter and the source-pack runtime's projector with
the four life-sciences features selected in the Science composition plan and sockets blocked. A protein reaches its
cited reference records, structures and published bioactivity with versions, reviewable cross-source identity and
cross-pack links. Offline evidence only, never live coverage (``docs/development/life-sciences-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_links import LifeSciLinks
from src.kb.lifesci_monitoring import LifeSciMonitor
from src.kb.lifesci_queries import LifeSciQueries
from src.kb.lifesci_records import (
    EXCLUSIONS,
    INFERENCE_KEYS,
    keys_in,
    personal_fields,
    record_id_for,
)
from src.kb.lifesci_store import LifeSciProjector, LifeSciStore, readiness
from tests.unit import lifesci_harness as h
from tests.unit.composition.test_migration import _migrated

FEATURES = ["life-sciences-uniprot", "life-sciences-ncbi", "life-sciences-pdb", "life-sciences-chembl"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_protein_to_cited_reference_records_structures_and_published_bioactivity():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("science", bundles["science"]["version"], features=FEATURES)
    assert coordinator.activate("lifesci-acceptance")["status"] == "published"
    assert all(readiness(conn)["features"].values())
    assert PROJECTORS["noesis-lifesci-record-v2"](conn).__class__ is LifeSciProjector

    # Pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "life-sciences"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": ours})
    assert replay["valid"] and replay["coverage"]["verified"] == 5

    # First releases, then UniProt 2099_02, a later NCBI Gene pull, a PDB weekly release and ChEMBL CHEMBL_100;
    # synthetic Chemicals, Clinical, Biodiversity and literature records stand in for the other packs.
    h.load_all(conn, revisions=True)
    h.seed_all(conn)
    store = LifeSciStore(conn, initialize=False)
    assert readiness(conn)["stores_ready"] is True

    # Revision history: entry and sequence versions, a merge, a replaced gene, PDB revisions, an obsolete entry.
    exa1 = record_id_for(h.NS, "uniprot", "protein", "X9EXA1")
    assert [r["marker"] for r in store.revisions(h.NS, exa1)] == ["entry-1", "entry-2"]
    assert [r["status"] for r in store.revisions(h.NS, record_id_for(h.NS, "uniprot", "protein", "X9EXA2"))] == [
        "active", "merged"]
    assert [r["marker"] for r in store.revisions(h.NS, record_id_for(h.NS, "rcsb-pdb", "structure", "9EXA"))] == [
        "rev-1.0", "rev-1.1"]

    # Reviewable identity: published cross-references first, nothing accepted until reviewed.
    identity = LifeSciIdentity(conn, now=lambda: h.SECOND + 1)
    proposed = identity.propose(h.NS, scopes=h.SCOPES, principal_id="matcher")
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    for match in proposed["matches"]:
        if match["method"] == "scientific-name":
            identity.review(h.NS, match["match_id"], "rejected", "a name alone does not identify a taxon",
                            scopes=h.SCOPES, principal_id="reviewer")
        else:
            identity.review(h.NS, match["match_id"], "accepted", "published identifier checked", scopes=h.SCOPES,
                            principal_id="reviewer")
    assert {r["native_id"] for r in identity.unmatched(h.NS, scopes=h.READ_ONLY, record_type="taxon")["unmatched"]} \
        >= {"99000100"}

    # Cross-pack links by accepted match, shared identifier and citation.
    linked = LifeSciLinks(conn, now=lambda: h.SECOND + 2).link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    assert {o: r["status"] for o, r in linked["owners"].items()} == {
        "chemicals": "linked", "clinical": "linked", "biodiversity": "linked", "literature": "linked"}
    assert {(x["owner"], x["basis"]) for x in linked["linked"]} >= {
        ("chemicals", "accepted-match"), ("clinical", "shared-identifier"), ("clinical", "citation"),
        ("biodiversity", "accepted-match"), ("literature", "citation")}

    # As-of answers: the protein at UniProt 2099_01 with its cross-reference graph, cited per version.
    queries = LifeSciQueries(conn, now=lambda: h.SECOND + 3)
    entry = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="2099_01")
    assert entry["entry"]["version"]["marker"] == "entry-1"
    graph = entry["xref_graph"]
    assert {(x["database"], x["id"], x["asserted_by"]) for x in graph["outgoing"]} >= {
        ("PDB", "9EXA", "uniprot"), ("GeneID", "990000101", "uniprot"), ("ChEMBL", "CHEMBL9900001", "uniprot")}
    assert {(x["source"], x["native_id"]) for x in graph["incoming"]} == {
        ("rcsb-pdb", "9EXA"), ("rcsb-pdb", "9EXC"), ("chembl", "CHEMBL9900001")}
    assert {m["state"] for m in graph["identity_matches"]} == {"accepted"}
    merged = queries.entry(h.NS, "X9EXA2", scopes=h.READ_ONLY)
    assert merged["resolved_to"][0]["native_id"] == "X9EXA1" and merged["resolved_to"][0]["citation"]

    # Published bioactivity for the protein's target per ChEMBL release, never converted or aggregated.
    old = queries.compounds_for_target(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="CHEMBL_99")
    new = queries.compounds_for_target(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="CHEMBL_100")
    count = lambda answer: sum(len(g["activities"]) for g in answer["groups"])
    assert (count(old), count(new)) == (3, 4)
    ki = next(g for g in new["groups"] if g["activity_type"] == "Ki")["activities"][0]
    assert (ki["standard"]["relation"], ki["data_validity_comment"]) == (">", "Potential transcription error")

    # A subject with no records.
    assert queries.entry(h.NS, "X9ZZZ9", scopes=h.READ_ONLY)["status"] == "none_on_record"

    # Evidence bundle: every item cites source, record revision and as-of time.
    bundle = queries.export_bundle(entry, created_at_ms=h.SECOND + 4)
    assert bundle["items"] and all(i["source"] and i["record_revision"] and i["as_of"] for i in bundle["items"])

    # Exclusions and the minimisation decision hold across every answer.
    for answer in (entry, merged, old, new, bundle):
        assert personal_fields(answer) == [] and keys_in(answer, INFERENCE_KEYS) == []
        assert "Quell" not in json.dumps(answer)
    assert list(EXCLUSIONS) == entry["exclusions"]

    # A target monitor: a restart replays without duplicates.
    monitor = LifeSciMonitor(conn, now=lambda: h.SECOND + 5)
    sub = monitor.create(h.NS, "target", watch={"target": "X9EXA1"}, principal_id="alice",
                         scopes=h.SCOPES)["subscription_id"]
    first = monitor.run(sub, principal_id="alice", scopes=h.SCOPES)
    assert {n["kind"] for n in first["notifications"]} >= {"new_activity", "activity_revised"}
    assert LifeSciMonitor(conn, now=lambda: h.SECOND + 6).run(sub, principal_id="alice",
                                                              scopes=h.SCOPES)["notifications"] == []

    # Re-acquisition is idempotent: nothing new is recorded.
    before = conn.execute("SELECT count(*) FROM lifesci_revisions").fetchone()[0]
    h.load_all(conn, revisions=True)
    assert conn.execute("SELECT count(*) FROM lifesci_revisions").fetchone()[0] == before
