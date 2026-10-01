"""Life-science records: contract, minimisation at write time, revision chains and as-of lookup (#2652, LS02 #2661)."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from src.kb.lifesci_records import (
    CONTRACT,
    LifeSciError,
    record_id_for,
    release_order,
    validate_statement,
)
from src.kb.lifesci_store import LifeSciStore
from tests.unit import lifesci_harness as h


def protein(entry_version=1, *, release="2099_01", published_on="2099-01-12", **attributes):
    return {
        "contract": CONTRACT, "record_type": "protein", "source": "uniprot", "native_id": "X9TST1",
        "release": {"label": release, "published_on": published_on, "basis": "provider"},
        "version": {"marker": f"entry-{entry_version}", "basis": "entry-version", "order": [entry_version],
                    "date": None},
        "status": "active", "status_published": "UniProtKB reviewed (Swiss-Prot)", "successors": [],
        "label": "Test protein (fictional)",
        "attributes": {"entry_version": entry_version, "sequence_version": 1, "reviewed": "reviewed",
                       "sequence": {"value": "MEX", "length": 3}, **attributes},
        "xrefs": [{"database": "PDB", "id": "9TST"}], "citations": [{"kind": "doi", "id": "10.99999/t"}],
        "licence": {"id": "CC-BY-4.0", "attribution": "UniProt"}, "url": "https://rest.uniprot.org/x",
    }


def store():
    return LifeSciStore(h.connection(), now=lambda: h.FIRST)


def test_the_contract_schema_is_draft7_and_accepts_every_fixture_statement():
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-lifesci-record-v1.json").read_text())
    jsonschema.Draft7Validator.check_schema(schema)
    for provider in h.SOURCES:
        for records, _ in h.fetch(provider):
            for record in records:
                assert validate_statement(record["lifesci_statement"]) == record["lifesci_statement"]


@pytest.mark.parametrize("field,value", [
    ("authors", ["Quell M."]), ("audit_author", [{"name": "Quell, M."}]), ("email", "someone@example.org"),
    ("orcid", "0000-0000-0000-0000"),
])
def test_personal_fields_are_refused_at_write_time_anywhere_in_a_statement(field, value):
    statement = protein()
    statement["attributes"]["nested"] = {field: value}
    with pytest.raises(LifeSciError) as exc:
        store().apply(h.NS, statement)
    assert exc.value.code == "personal_data"


@pytest.mark.parametrize("field", ["pchembl_value", "predicted_activity", "druggability", "sequence_alignment"])
def test_inferred_predicted_or_converted_values_are_refused(field):
    statement = protein()
    statement["attributes"][field] = "1"
    with pytest.raises(LifeSciError) as exc:
        validate_statement(statement)
    assert exc.value.code == "excluded_field"


def test_record_rules_as_published():
    merged = protein()
    merged.update(status="merged", successors=[])
    with pytest.raises(LifeSciError, match="successor"):
        validate_statement(merged)
    missing = protein()
    del missing["attributes"]["sequence_version"]
    with pytest.raises(LifeSciError, match="sequence_version"):
        validate_statement(missing)
    wrong = protein()
    wrong["source"] = "chembl"
    with pytest.raises(LifeSciError):
        validate_statement(wrong)


def test_revision_chain_as_of_release_and_date_and_replay():
    s = store()
    first = s.apply(h.NS, protein(1))
    assert first["status"] == "created"
    assert s.apply(h.NS, protein(1))["status"] == "unchanged"
    second = s.apply(h.NS, protein(2, release="2099_02", published_on="2099-03-09"))
    assert second["status"] == "revised"
    # The same entry version seen again in a later release keeps one revision and grows its release membership.
    assert s.apply(h.NS, protein(2, release="2099_03", published_on="2099-05-04"))["status"] == "unchanged"
    record_id = first["record_id"]
    assert [r["marker"] for r in s.revisions(h.NS, record_id)] == ["entry-1", "entry-2"]
    assert [m["release_label"] for m in s.releases(h.NS, record_id)] == ["2099_01", "2099_02", "2099_03"]
    assert s.in_force(h.NS, record_id, release="2099_01")["marker"] == "entry-1"
    assert s.in_force(h.NS, record_id, release="2099_02")["marker"] == "entry-2"
    assert s.in_force(h.NS, record_id, as_of="2099-02-01")["marker"] == "entry-1"
    assert s.in_force(h.NS, record_id, as_of="2099-01-01") is None
    assert s.in_force(h.NS, record_id)["marker"] == "entry-2"


def test_a_late_older_release_is_history_and_a_changed_payload_under_one_marker_is_a_conflict():
    s = store()
    s.apply(h.NS, protein(2, release="2099_02"))
    late = s.apply(h.NS, protein(1, release="2099_01"))
    assert late["status"] == "history"
    assert s.in_force(h.NS, late["record_id"])["marker"] == "entry-2"
    changed = protein(2, release="2099_02")
    changed["label"] = "Silently changed (fictional)"
    conflict = s.apply(h.NS, changed)
    assert conflict["status"] == "conflict"
    assert s.statement(h.NS, conflict["revision_id"])["label"] == "Test protein (fictional)"
    assert [c["marker"] for c in s.conflicts(h.NS)] == ["entry-2"]


def test_a_removal_by_the_source_is_a_revision_never_a_deletion():
    s = store()
    first = s.apply(h.NS, protein(1))
    removed = copy.deepcopy(protein(1, release="2099_02"))
    removed.update(status="deleted", version={"marker": "content-deleted", "basis": "content", "order": None,
                                              "date": None}, xrefs=[], citations=[])
    removed["attributes"] = {"inactive_reason": "DELETED"}
    assert s.apply(h.NS, removed)["status"] == "revised"
    chain = s.revisions(h.NS, first["record_id"])
    assert [r["status"] for r in chain] == ["active", "deleted"]
    assert s.in_force(h.NS, first["record_id"], release="2099_01")["status"] == "active"


def test_fixture_chains_keep_versions_successors_and_release_membership():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    s = LifeSciStore(conn, initialize=False)
    exa1 = record_id_for(h.NS, "uniprot", "protein", "X9EXA1")
    chain = s.revisions(h.NS, exa1)
    assert [(s.statement(h.NS, r["revision_id"])["attributes"]["entry_version"],
             s.statement(h.NS, r["revision_id"])["attributes"]["sequence_version"]) for r in chain] == [(1, 1), (2, 2)]
    exa2 = record_id_for(h.NS, "uniprot", "protein", "X9EXA2")
    assert [(r["status"], r["successors"]) for r in s.revisions(h.NS, exa2)] == [("active", []),
                                                                                ("merged", ["X9EXA1"])]
    assert s.successions(h.NS, "uniprot", "X9EXA2")[0]["decision_id"]
    pdb = record_id_for(h.NS, "rcsb-pdb", "structure", "9EXA")
    latest = s.statement(h.NS, s.in_force(h.NS, pdb)["revision_id"])
    assert [x["minor_revision"] for x in latest["attributes"]["revision_history"]] == [0, 1]
    target = record_id_for(h.NS, "chembl", "target", "CHEMBL9900001")
    assert len(s.revisions(h.NS, target)) == 1 and len(s.releases(h.NS, target)) == 2
    assert release_order("CHEMBL_100") > release_order("CHEMBL_99")
