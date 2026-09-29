"""H08: trial design in methodology provenance and outcome-switching findings."""

import duckdb
import pytest

from src.kb.clinical_methodology import TrialMethodology
from src.kb.clinical_records import ClinicalRecordStore
from src.kb.methodology_provenance import MethodologyError, MethodologyStore, trial_design
from tests.unit.clinical.harness import NS, Env

SCOPES = {"knowledge:methodology:read", "knowledge:methodology:write", "knowledge:methodology:extract"}


def _design(primary, secondary=None, **over):
    base = {"phase": ["phase-3"], "allocation": "randomized", "masking": "double", "sample_size_planned": 100,
            "sample_size_actual": None, "preregistration": {"prospective": True},
            "primary_outcomes": primary, "secondary_outcomes": secondary}
    base.update(over)
    return base


def _register(store, versions):
    for generation, design in enumerate(versions):
        store.register_trial_revision("ns", "ctgov:NCT09000009", f"registry-history:{generation}", "Synthetic",
                                      design, principal_id="p", scopes=SCOPES, generation=generation,
                                      observed_at_ms=generation + 1)
    return "study:" + __import__("src.kb.methodology_provenance", fromlist=["_digest"])._digest(
        ["ns", "ctgov:NCT09000009"])[:24]


def test_trial_design_fields_are_recorded_per_revision_with_unknowns():
    fields = trial_design({"phase": None, "allocation": "unknown", "masking": "none", "sample_size_planned": None,
                           "sample_size_actual": 12, "preregistration": {"prospective": None},
                           "primary_outcomes": [{"measure": "HbA1c", "time_frame": "Week 12"}]})
    assert fields["unknown"] == ["allocation", "phase", "preregistration", "sample_size_planned"]
    with pytest.raises(MethodologyError):
        trial_design({"sample_size_planned": -1})
    with pytest.raises(MethodologyError):
        trial_design({"dose": "10 mg"})


def test_added_removed_retimed_and_demoted_primary_outcomes_are_findings_citing_both_versions():
    store = MethodologyStore(duckdb.connect(), now=lambda: 1)
    a = {"measure": "HbA1c change", "time_frame": "Week 26"}
    b = {"measure": "Body weight change", "time_frame": "Week 26"}
    c = {"measure": "Fasting glucose", "time_frame": "Week 26"}
    study = _register(store, [
        _design([a, b]),
        _design([{**a, "time_frame": "Week 52"}, c], secondary=[b]),  # re-timed, demoted, added
        _design([c]),                                                 # removed
    ])
    report = store.outcome_switching("ns", study, scopes=SCOPES)
    kinds = sorted((f["kind"], f["measure"]) for f in report["findings"])
    assert kinds == [("primary-added", "Fasting glucose"), ("primary-demoted", "Body weight change"),
                     ("primary-removed", "HbA1c change"), ("primary-retimed", "HbA1c change")]
    for finding in report["findings"]:
        assert finding["finding"] is True and "not a judgement" in finding["note"]
        assert finding["before"]["study_revision_id"] != finding["after"]["study_revision_id"]
        assert finding["before"]["registry_version"] and finding["after"]["registry_version"]
    publication = store.outcome_switching("ns", study, scopes=SCOPES, publication_statements=[
        {"text": "The primary outcome was change in fasting glucose at week 12.",
         "locator": {"document_id": "d", "passage": 2}}])
    assert publication["findings"][-1]["kind"] == "publication-retimed"
    assert publication["findings"][-1]["after"]["publication"]["document_id"] == "d"


def test_non_trial_studies_and_existing_methodology_work_unchanged():
    store = MethodologyStore(duckdb.connect(), now=lambda: 1)
    study = store.register_study("ns", "doi:10.5555/x", "1", "Cohort", {"type": "cohort"}, {}, [], [], [],
                                 principal_id="p", scopes=SCOPES)
    assert store.outcome_switching("ns", study["study_id"], scopes=SCOPES)["applies"] is False
    assert store.trial_revisions("ns", study["study_id"], scopes=SCOPES) == []
    graph = store.replication_graph("ns", study["study_id"], scopes=SCOPES)
    assert graph["links"] == [] and store.explain_strength("ns", study["study_id"], scopes=SCOPES)["strength"] == "unknown"


def test_registry_versions_are_synced_idempotently_and_publication_outcomes_extracted():
    env = Env()
    env.acquire("r1", ["ctgov-trial-history"])
    env.seed_publications()
    rid = ClinicalRecordStore(env.conn).trial_id(NS, "ctgov", "NCT09000001")
    methods = TrialMethodology(env.conn)
    first = methods.sync(NS, rid, principal_id="alice", scopes=env.scopes())
    again = methods.sync(NS, rid, principal_id="alice", scopes=env.scopes())
    assert [r["version"] for r in first["revisions"]] == [f"registry:registry-history:{v}" for v in range(4)]
    assert all(r["idempotent"] for r in again["revisions"])
    v0, v3 = first["revisions"][0]["trial"], first["revisions"][-1]["trial"]
    assert (v0["sample_size_planned"], v0["sample_size_actual"]) == (600, None)
    assert (v3["sample_size_planned"], v3["sample_size_actual"]) == (600, 612)  # planned carried from registration
    assert v3["allocation"] == "randomized" and v3["masking"] == "quadruple" and v3["phase"] == ["phase-3"]
    assert v3["preregistration"]["prospective"] is True
    documents = [d for d in env.publication_documents() if d["document_id"] == env.documents["pubmed:99000001"]]
    report = methods.outcome_switching(NS, rid, principal_id="alice", scopes=env.scopes(), documents=documents)
    kinds = [f["kind"] for f in report["findings"]]
    assert kinds == ["primary-retimed", "publication-retimed"]
    retimed = report["findings"][0]
    assert (retimed["before"]["time_frame"], retimed["after"]["time_frame"]) == ("Baseline, week 26",
                                                                                 "Baseline, week 52")
    assert (retimed["before"]["registry_version"], retimed["after"]["registry_version"]) == (
        "registry:registry-history:1", "registry:registry-history:2")
    assert report["findings"][1]["after"]["publication"]["document_id"] == env.documents["pubmed:99000001"]
