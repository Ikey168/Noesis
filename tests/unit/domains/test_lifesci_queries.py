"""Entry as of a release with its cross-reference graph, and published activity against a target (#2696, #2701)."""

from __future__ import annotations

import pytest

from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_queries import LifeSciQueries, export_bundle
from src.kb.lifesci_records import LifeSciError, forbidden_keys
from tests.unit import lifesci_harness as h


@pytest.fixture(scope="module")
def env():
    env = h.Env().loaded()
    env.later()
    LifeSciIdentity(env.conn).propose(h.NS, principal_id="alice", scopes=h.ALL)
    return env


def test_an_accession_resolves_to_the_version_in_force_at_a_release_with_citations(env):
    queries = LifeSciQueries(env.conn)
    first = queries.entry_as_of(h.NS, "P0DZZ1", scopes=h.READ, release="2099_01")
    (entry,) = first["entries"]
    assert entry["as_published"]["entry_version"] == 20 and entry["release"] == "2099_01"
    assert entry["label"] == "reviewed (Swiss-Prot)" and entry["version_in_force"]["entry_version"] == 20
    assert [x["entry_version"] for x in entry["history"]] == [20, 21]
    assert first["citations"][0]["revision_id"] == entry["revision_id"] and first["citations"][0]["retrieved_on"]
    later = queries.entry_as_of(h.NS, "P0DZZ1", scopes=h.READ, release="2099_02")
    assert later["entries"][0]["as_published"]["sequence_version"] == 3
    older = queries.entry_as_of(h.NS, "P0DZZ1", scopes=h.READ, release="2098_05")
    assert older["entries"][0]["event"] == "version-only"
    assert older["entries"][0]["version_in_force"]["entry_version"] == 19
    assert older["unknowns"][0]["kind"] == "full_entry_not_acquired"
    by_date = queries.entry_as_of(h.NS, "P0DZZ1", scopes=h.READ, as_of="2099-01-31")
    assert by_date["entries"][0]["as_published"]["entry_version"] == 20


def test_obsolete_merged_replaced_and_superseded_identifiers_resolve_with_history(env):
    queries = LifeSciQueries(env.conn)
    merged = queries.entry_as_of(h.NS, "Q9ZZZ1", scopes=h.READ, release="2099_02")
    assert merged["resolution"][0]["from"] == "uniprot:Q9ZZZ1" and merged["resolution"][0]["to"] == ["uniprot:P0DZZ1"]
    assert [e["subject_key"] for e in merged["entries"]] == ["uniprot:Q9ZZZ1", "uniprot:P0DZZ1"]
    assert merged["status"] == "answered" and len(merged["citations"]) == 2
    assert queries.entry_as_of(h.NS, "9ZZ1", scopes=h.READ)["resolution"][0]["to"] == ["pdb:9ZZ3"]
    assert queries.entry_as_of(h.NS, "99990009", scopes=h.READ)["resolution"][0]["to"] == ["ncbigene:99990001"]
    assert queries.entry_as_of(h.NS, "999002", scopes=h.READ)["resolution"][0]["to"] == ["ncbitaxon:999001"]
    missing = queries.entry_as_of(h.NS, "P0DZZ9", scopes=h.READ)
    assert missing["status"] == "not_on_record" and missing["entries"] == []


def test_cross_references_are_labelled_with_the_asserting_source_and_identity_state(env):
    answer = LifeSciQueries(env.conn).entry_as_of(h.NS, "P0DZZ1", scopes=h.READ)
    graph = answer["entries"][0]["cross_references"]
    outgoing = {o["subject_key"]: o for o in graph["outgoing"] if o["subject_key"]}
    assert outgoing["pdb:9ZZ2"]["asserted_by"] == "uniprot" and outgoing["pdb:9ZZ2"]["held"] is True
    assert outgoing["pdb:9ZZ2"]["identity"]["state"] == "proposed"
    assert outgoing["ncbitaxon:999001"]["field"] == "organism.taxon_id"
    incoming = {(i["subject_key"], i["asserted_by"]) for i in graph["incoming"]}
    assert {("pdb:9ZZ2", "pdb"), ("pdb:9ZZ3", "pdb"), ("ncbigene:99990001", "ncbi"),
            ("chembl-target:CHEMBL9990201", "chembl")} <= incoming
    assert forbidden_keys(answer) == []


def test_target_activities_are_listed_per_release_as_published_and_never_aggregated(env):
    queries = LifeSciQueries(env.conn)
    first = queries.target_activities(h.NS, "CHEMBL9990201", scopes=h.READ, release="ChEMBL_99")
    assert first["releases_on_record"] == ["ChEMBL_99", "ChEMBL_100"] and first["aggregation"] == "none"
    rows = {r["activity_id"]: r for r in first["activities"]}
    assert set(rows) == {"99990001", "99990002", "99990003"}
    assert rows["99990002"]["published"] == {"type": "Ki", "relation": ">", "value": "10", "units": "uM"}
    assert rows["99990002"]["data_validity_comment"] == "Outside typical range"
    assert rows["99990001"]["document"]["doi"] == "10.5555/fict.lifesci.2099.3"
    assert rows["99990003"]["document"]["doi"] is None and rows["99990003"]["activity_comment"] == "Active"
    assert rows["99990001"]["citation"]["release"] == "ChEMBL_99"
    assert first["activity_types"] == ["IC50", "Inhibition", "Ki"]
    latest = queries.target_activities(h.NS, "P0DZZ1", scopes=h.READ)
    assert latest["release"] == "ChEMBL_100" and latest["resolved_via"] == "published target component accession"
    assert {r["activity_id"] for r in latest["activities"]} == {"99990001", "99990003", "99990004"}
    assert [r["activity_id"] for r in latest["removed_in_release"]] == ["99990002"]
    assert queries.target_activities(h.NS, "CHEMBL9990999", scopes=h.READ)["status"] == "not_on_record"
    with pytest.raises(LifeSciError):
        queries.target_activities(h.NS, "CHEMBL9990201", scopes={"knowledge:lifesci:read"})


def test_evidence_bundles_cite_every_item_with_source_revision_and_as_of_time(env):
    queries = LifeSciQueries(env.conn)
    for answer in (queries.entry_as_of(h.NS, "Q9ZZZ1", scopes=h.READ, release="2099_01"),
                   queries.target_activities(h.NS, "CHEMBL9990201", scopes=h.READ, release="ChEMBL_99")):
        bundle = export_bundle(answer, created_at_ms=1)
        evidence = [o["payload"] for o in bundle["objects"] if o["payload"].get("kind") == "lifesci-record-revision"]
        assert {e["locator"]["revision_id"] for e in evidence} == {c["revision_id"] for c in answer["citations"]}
        assert all(e["source"] and e["as_of"]["retrieved_at_ms"] and e["url"] for e in evidence)
        assert bundle["completeness"]["status"] == "complete"
    partial = export_bundle(queries.entry_as_of(h.NS, "P0DZZ1", scopes=h.READ, release="2098_05"), created_at_ms=1)
    assert partial["completeness"]["status"] == "partial"
