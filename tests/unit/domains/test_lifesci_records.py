"""Life-science records: validation, minimisation at write time, revision chains and as-of lookup (#2652, LS02 #2661)."""

from __future__ import annotations

import json

import duckdb
import jsonschema
import pytest

from src.kb.lifesci_records import (
    CONTRACT,
    FORBIDDEN_KEYS,
    MINIMISATION,
    LifeSciError,
    forbidden_keys,
    release_order,
    statement,
    validate_statement,
)
from src.kb.lifesci_store import LifeSciStore, record_id_for
from tests.unit import lifesci_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-lifesci-record-v1.json").read_text())
SOURCE = {"url": "https://www.uniprot.org/uniprotkb/P0DZZ1/entry", "evidence_origin": "fixture"}


def protein(version=20, seq=2, release="2099_01", **extra):
    published = {"accession": "P0DZZ1", "entry_type": "UniProtKB reviewed (Swiss-Prot)", "entry_status": "active",
                 "reviewed": True, "entry_version": version, "sequence_version": seq, "release": release,
                 "sequence": {"value": "MKTAYIAKQR", "length": 10}, **extra}
    return statement("protein", "uniprot", "P0DZZ1", subject_name="Fictional kinase A", as_published=published,
                     source=SOURCE)


def activity(release="ChEMBL_99", value="12"):
    return statement("activity", "chembl", "99990001", subject_name="IC50", source={
        "url": "https://www.ebi.ac.uk/chembl/api/data/activity.json"}, as_published={
        "activity_id": "99990001", "release": release, "assay_chembl_id": "CHEMBL9990401",
        "target_chembl_id": "CHEMBL9990201", "molecule_chembl_id": "CHEMBL9990101",
        "document_chembl_id": "CHEMBL9990301", "published": {"type": "IC50", "relation": "=", "value": value,
                                                             "units": "nM"}})


def test_statements_keep_native_keys_versions_release_and_unknowns_and_match_the_contract():
    value = protein()
    assert value["contract"] == CONTRACT and value["subject"]["key"] == "uniprot:P0DZZ1"
    assert value["as_published"]["entry_version"] == 20 and value["as_published"]["sequence_version"] == 2
    assert "cross_references" in value["unknowns"]
    jsonschema.validate(value, SCHEMA)
    assert validate_statement(value) == value
    with pytest.raises(LifeSciError):
        statement("protein", "ncbi", "P0DZZ1", subject_name=None, as_published={}, source=SOURCE)
    with pytest.raises(LifeSciError):  # an active entry keeps its versions
        statement("protein", "uniprot", "P0DZZ1", subject_name=None, source=SOURCE, as_published={
            "accession": "P0DZZ1", "entry_type": "x", "entry_status": "active", "reviewed": True})
    with pytest.raises(LifeSciError):  # a merged entry names its successors
        statement("protein", "uniprot", "Q9ZZZ1", subject_name=None, source=SOURCE, as_published={
            "accession": "Q9ZZZ1", "entry_type": "Inactive", "entry_status": "obsolete",
            "inactive_reason": {"type": "MERGED", "successors": []}}, event="obsoleted")
    with pytest.raises(LifeSciError):  # computed structure models are not PDB IDs
        statement("structure", "pdb", "AF_AFP0DZZ1F1", subject_name=None, source=SOURCE,
                  as_published={"pdb_id": "AF_AFP0DZZ1F1", "status": "released"})


def test_every_fixture_statement_matches_the_json_schema():
    env = h.Env().loaded()
    env.later()
    for record in env.store.records(h.NS):
        for revision in env.store.revisions(h.NS, record["record_id"]):
            jsonschema.validate(revision["statement"], SCHEMA)


def test_minimisation_is_enforced_at_write_time():
    assert MINIMISATION["decision"] == "no personal data stored"
    for field in ("authors", "audit_author", "submitter", "email"):
        with pytest.raises(LifeSciError) as caught:
            protein(citations=[{"doi": "10.5555/x", field: ["A. Person"]}])
        assert caught.value.code in {"personal_field", "invalid_lifesci_record"}
        with pytest.raises(LifeSciError) as caught:
            statement("document", "chembl", "CHEMBL9990301", subject_name=None, source={
                "url": "https://www.ebi.ac.uk/chembl/", field: "A. Person"}, as_published={
                "document_chembl_id": "CHEMBL9990301", "release": "ChEMBL_99"})
        assert caught.value.code == "personal_field"
    assert forbidden_keys({"a": [{"authors": 1}]}) == ["a.authors"]


def test_predictions_conversions_and_aggregates_are_never_stored():
    assert {"pchembl_value", "predicted_activity", "mean_value"} <= FORBIDDEN_KEYS
    with pytest.raises(LifeSciError) as caught:
        protein(sequence={"value": "MK", "isoelectric_point": 7.1})
    assert caught.value.code == "forbidden_field"
    with pytest.raises(LifeSciError) as caught:  # a value is kept as the published text
        activity(value=12.0)
    assert caught.value.code == "converted_value"


def test_revision_chains_are_immutable_and_as_of_lookup_uses_release_and_retrieval_time():
    store = LifeSciStore(duckdb.connect(":memory:"))
    first = store.observe("ns", [protein()], observed_at_ms=1_000)
    assert first["counts"] == {"created": 1, "revised": 0, "unchanged": 0}
    assert store.observe("ns", [protein()], observed_at_ms=2_000)["counts"]["unchanged"] == 1
    store.observe("ns", [protein(21, 3, "2099_02")], observed_at_ms=3_000)
    # re-reading the older release adds nothing (release-scoped de-duplication)
    assert store.observe("ns", [protein()], observed_at_ms=4_000)["counts"]["unchanged"] == 1
    record_id = record_id_for("ns", "uniprot", "protein", "P0DZZ1")
    chain = store.revisions("ns", record_id)
    assert [r["revision_no"] for r in chain] == [1, 2] and chain[1]["supersedes"] == chain[0]["revision_id"]
    assert store.at_release("ns", record_id, "2099_01")["statement"]["as_published"]["entry_version"] == 20
    assert store.at_release("ns", record_id, "2099_05")["statement"]["as_published"]["entry_version"] == 21
    assert store.at_release("ns", record_id, "2098_12") is None
    assert store.current("ns", record_id, as_of_ms=2_500)["revision_no"] == 1
    assert store.current("ns", record_id)["revision_no"] == 2
    assert release_order("ChEMBL_100") > release_order("ChEMBL_99")


def test_removals_and_corrections_are_revisions_never_deletions():
    store = LifeSciStore(duckdb.connect(":memory:"))
    first = store.observe("ns", [activity()], observed_at_ms=1)
    record_id = first["results"][0]["record_id"]
    store.close_snapshot("ns", selection_key="k", provider="chembl", release="ChEMBL_99", complete=True,
                         present_record_ids=[record_id], url=None, run_id="r1", source_id="s")
    closed = store.close_snapshot("ns", selection_key="k", provider="chembl", release="ChEMBL_100", complete=True,
                                  present_record_ids=[], url=None, run_id="r2", source_id="s")
    assert closed["removed"] == [record_id]
    chain = store.revisions("ns", record_id)
    assert [r["event"] for r in chain] == ["published", "removed"]
    assert store.at_release("ns", record_id, "ChEMBL_99")["event"] == "published"
    assert store.at_release("ns", record_id, "ChEMBL_100")["event"] == "removed"
    truncated = store.close_snapshot("ns", selection_key="k2", provider="chembl", release="ChEMBL_100",
                                     complete=False, present_record_ids=[], url=None, run_id="r3", source_id="s")
    assert truncated["removed"] == []
