"""H02: clinical record kinds, revisions per registry version, links without merging, schemas."""

import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion import clinical_providers as cp
from src.kb.clinical_records import (
    COUNT_SEMANTICS,
    ClinicalRecordError,
    ClinicalRecordStore,
    record,
    record_id,
    register_schemas,
    validate_record,
)
from tests.unit.clinical.harness import load

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {"knowledge:clinical:read", "knowledge:clinical:write", "knowledge:ingestion:execute",
          "namespace:ns:read", "namespace:ns:write"}
V = {"version": "1", "date": "2020-01-01", "basis": "registry-history"}


def trial(**over):
    fields = dict(registry="ctgov", identifier="NCT09000001", title="Fixture", source_url="https://clinicaltrials.gov/study/"
                  "NCT09000001", native_version=V, status={"normalized": "recruiting"},
                  design={"allocation": "randomized", "masking": "double", "enrollment_planned": 10})
    fields.update(over)
    return record("registered-trial", **fields)


def history_records():
    versions = cp.parse_ctgov_history(load("ctgov_NCT09000001_history.json"))
    return cp.parse_ctgov_versions("NCT09000001", versions,
                                   [load(f"ctgov_NCT09000001_v{v}.json") for v in range(4)],
                                   load("ctgov_NCT09000001.json"))[0]


def test_record_kinds_keep_registration_results_and_publication_apart():
    with pytest.raises(ClinicalRecordError) as caught:
        trial(outcome_results=[])
    assert caught.value.code == "unsupported" or "outcome_results" in str(caught.value)
    with pytest.raises(ClinicalRecordError):
        record("result-posting", registry="ctgov", identifier="NCT09000001", source_url="https://clinicaltrials.gov/x",
               native_version=V, posted={}, content_acquired=True, doi="10.5555/x")
    with pytest.raises(ClinicalRecordError) as caught:
        record("registry-link", link_kind="registry-publication", from_record={"provider": "ctgov",
                                                                            "identifier": "NCT09000001"},
               to={"pmid": "1"}, evidence_kind="registry-declared-reference", evidence={"x": 1}, status="accepted")
    assert caught.value.code == "invalid_publication_ref"  # a publication is a documents revision, not a record


def test_no_record_carries_advice_dosing_incidence_or_grade():
    for key in ("dose", "incidence", "recommendation", "grade", "causation"):
        with pytest.raises(ClinicalRecordError) as caught:
            validate_record({**trial(), "design": {**trial()["design"], key: "x"}})
        assert caught.value.code == "outside_boundary"
    counts = cp.parse_openfda_event_counts(load("openfda_event_counts.json"), generic_name="noetiglutide",
                                           count_field="patient.reaction.reactionmeddrapt.exact")
    assert counts["count_semantics"] == COUNT_SEMANTICS and counts["reports_total"] is None
    assert "reports_total" in counts["unknowns"]
    with pytest.raises(ClinicalRecordError):
        validate_record({**counts, "count_semantics": "incidence per 100 patients"})
    with pytest.raises(ClinicalRecordError) as caught:
        validate_record({k: v for k, v in counts.items() if k != "disclaimer"})
    assert caught.value.code == "missing_disclaimer"


def test_unknowns_stay_unknown_and_identifiers_are_validated():
    item = trial(design={}, status={}, sponsor={})
    assert {"design.allocation", "design.masking", "design.enrollment_planned", "status", "sponsor",
            "registration.prospective", "results_indicator"} <= set(item["unknowns"])
    with pytest.raises(ClinicalRecordError) as caught:
        trial(identifier="NCT123")
    assert caught.value.code == "invalid_identifier"
    with pytest.raises(ClinicalRecordError):
        trial(secondary_identifiers=[{"kind": "eudract", "value": "2015-1-10"}])


def test_each_registry_version_is_a_revision_with_its_date_and_history(tmp_path):
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn, now=lambda: 1)
    summary = store.ingest("ns", "ctgov", history_records(), observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    rid = store.trial_id("ns", "ctgov", "NCT09000001")
    history = store.history("ns", rid, scopes=SCOPES)
    assert [(r["version_key"], r["version_date"]) for r in history["revisions"]] == [
        ("registry-history:0", "2015-11-20"), ("registry-history:1", "2016-06-03"),
        ("registry-history:2", "2017-11-20"), ("registry-history:3", "2019-02-14")]
    assert history["revisions"][1]["amendments"][0]["kind"] in {"new_registry_version", "status_change"}
    assert {a["kind"] for a in history["revisions"][1]["amendments"]} >= {"status_change", "new_registry_version"}
    # Replaying the same observation creates nothing.
    again = store.ingest("ns", "ctgov", history_records(), observation_id="o2", observed_at_ms=2, scopes=SCOPES)
    assert not again["created"] and not again["revised"] and summary["created"]
    outcome = next(r for r in store.find("ns", scopes=SCOPES, kinds={"outcome-measure"})
                   if r["record"]["role"] == "primary")
    frames = [r["record"]["time_frame"] for r in store.history("ns", outcome["record_id"], scopes=SCOPES)["revisions"]]
    assert frames == ["Baseline, week 26", "Baseline, week 26", "Baseline, week 52", "Baseline, week 52"]


def test_same_registry_version_with_different_content_is_kept_as_a_conflict():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn, now=lambda: 1)
    store.ingest("ns", "ctgov", history_records(), observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    versions = cp.parse_ctgov_history(load("ctgov_NCT09000001_history.json"))
    conflicting, _ = cp.parse_ctgov_versions("NCT09000001", versions,
                                             [load(f"ctgov_NCT09000001_v{v}.json") for v in range(4)],
                                             load("ctgov_NCT09000001_conflicting_current.json"))
    summary = store.ingest("ns", "ctgov", conflicting, observation_id="o2", observed_at_ms=2, scopes=SCOPES)
    rid = store.trial_id("ns", "ctgov", "NCT09000001")
    assert summary["conflicts"] and summary["conflicts"][0]["record_id"] == rid
    history = store.history("ns", rid, scopes=SCOPES)
    assert len(history["revisions"]) == 4  # the retained version is not replaced
    assert history["version_conflicts"][0]["conflicting_record"]["sponsor"]["lead"] == "Fixture Therapeutics AG"


def test_cross_registry_records_are_linked_with_evidence_never_merged():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn, now=lambda: 1)
    store.ingest("ns", "ctgov", history_records(), observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    euctr = cp.parse_euctr_text(load("euctr_2015-900001-10.txt"), eudract="2015-900001-10")["records"]
    store.ingest("ns", "euctr", euctr, observation_id="o2", observed_at_ms=2, scopes=SCOPES)
    rid = store.trial_id("ns", "ctgov", "NCT09000001")
    other = store.trial_id("ns", "euctr", "2015-900001-10")
    assert rid != other
    view = store.cross_registry("ns", rid, scopes=SCOPES)
    assert view[0]["merged"] is False and view[0]["record_id"] == other
    kinds = {e["evidence"]["declared_as"] for e in view[0]["link_evidence"]}
    assert kinds == {"eudract", "nct"}  # both registries declare each other
    assert all(e["evidence"]["locator"] for e in view[0]["link_evidence"])
    fields = {d["field"] for d in view[0]["disagreements"]}
    assert "primary_outcomes" in fields  # wording differs; both kept with their revisions


def test_namespaces_isolate_records():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn, now=lambda: 1)
    store.ingest("ns", "ctgov", history_records(), observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    other = {"knowledge:clinical:read", "namespace:other:read"}
    assert store.find("other", scopes=other) == []
    rid = store.trial_id("ns", "ctgov", "NCT09000001")
    with pytest.raises(ClinicalRecordError):
        store.get("other", rid, scopes=other)
    with pytest.raises(ClinicalRecordError):
        store.get("ns", rid, scopes=other)
    assert record_id("ns", trial()) != record_id("other", trial())


def test_schemas_are_registered_and_fixture_records_validate():
    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = register_schemas(conn, principal_id="svc", scopes=scopes)
    assert {m["name"] for m in modules} == {"noesis-clinical-record", "noesis-clinical-evidence-map"}
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    records = history_records() + cp.parse_ctis_trial(load("ctis_2023-509001-12-00.json"))["records"]
    records += cp.parse_openfda_labels(load("openfda_label_v7.json"), query="q")
    records.append(cp.parse_prospero_export(load("prospero_CRD42099000001.json"), supplied_by="alice"))
    for item in records:
        result = registry.validate_instance({"kind": "schema", "name": "noesis-clinical-record", "version": "1.0.0"},
                                            item, scopes=scopes)
        assert result["valid"], result["errors"]
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-clinical-record-v1.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**history_records()[0], "dose": "x"}, schema)
    impact = conn.execute("SELECT consumer_id FROM knowledge_schema_dependencies").fetchall()
    assert ("clinical-evidence",) in impact
