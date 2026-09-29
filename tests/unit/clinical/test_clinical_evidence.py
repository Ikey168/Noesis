"""H09: explained strength-of-evidence view, separate from claim conclusions, never a grade without inputs."""

import json

import jsonschema
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.clinical_evidence import DESIGN_RULES, NON_ADVICE, SUMMARY_RULES, EvidenceMapService, validate_question
from src.kb.clinical_records import ClinicalRecordError
from src.kb.methodology_provenance import MethodologyStore
from tests.unit.clinical.harness import NS, QUESTION, ROOT, Env

FORBIDDEN = {"recommendation", "recommended_dose", "dose", "dosing", "verdict", "conclusion", "advice", "incidence"}


@pytest.fixture(scope="module")
def built():
    env = Env()
    env.journey()
    return env, env.build_map()


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_view_lists_trials_reviews_design_results_retractions_switching_and_unknowns(built):
    _, view = built
    trials = {t["identifier"]: t for t in view["trials"]}
    assert set(trials) == {"NCT09000001", "NCT09000002", "NCT09000003", "2023-509001-12-00", "2015-900001-10"}
    t1, t2 = trials["NCT09000001"], trials["NCT09000002"]
    assert t1["design"]["allocation"] == "randomized" and t1["design"]["sample_size_actual"] == 612
    assert t1["results"]["state"] == "posted" and t2["results"]["state"] == "not-posted"
    assert "no results posting is recorded" in t2["results"]["note"]
    assert t2["retracted"] is True and t1["retracted"] is False
    assert [f["kind"] for f in t1["outcome_switching"]] == ["primary-retimed", "publication-retimed"]
    assert trials["2015-900001-10"]["results"]["state"] == "available-not-acquired"
    assert "design.enrollment_actual" in trials["2023-509001-12-00"]["unknowns"]
    assert view["reviews"][0]["identifier"] == "CRD42099000001"
    assert "not review findings" in view["reviews"][0]["note"]
    assert len(t1["versions"]) >= 4


def test_every_category_is_rule_based_and_traceable(built):
    _, view = built
    strength = view["strength"]
    assert strength["score"] is None
    assert strength["rules"]["design"] == DESIGN_RULES and strength["rules"]["summary"] == SUMMARY_RULES
    rows = {r["identifier"]: r for r in strength["trials"]}
    assert rows["NCT09000001"]["design_rule"] == "D1" and rows["NCT09000002"]["design_rule"] == "D2"
    assert rows["NCT09000001"]["design_inputs"]["allocation"] == "randomized"
    for trial in view["trials"]:
        category = trial["design"]["category"]
        assert category["source"] == {"record_id": trial["record_id"], "revision": trial["revision"]}
        assert category["definition"] == DESIGN_RULES[category["rule_id"]]["rule"]
    # One D1 trial with posted results whose publication is not retracted -> S2.
    assert strength["summary"]["rule"] == "S2"
    assert {i["record_id"] for i in strength["summary"]["inputs"]} == {t["record_id"] for t in view["trials"]}
    assert strength["counts"]["retracted"] == 1 and strength["counts"]["results_not_posted"] == 3


def test_no_grade_is_asserted_without_recorded_inputs(built):
    env, view = built
    grade = view["strength"]["grade"]
    assert grade["asserted"] is False
    assert grade["inputs_missing"] == ["risk of bias", "inconsistency", "indirectness", "imprecision",
                                       "publication bias"]
    # A single recorded domain still leaves GRADE unasserted, with that input shown as recorded.
    study = view["trials"][0]["design"]["methodology"]["study_id"]
    MethodologyStore(env.conn).assess(NS, study, "GRADE", "risk of bias", "low", "reviewer judgement",
                                      principal_id="rev", scopes={"knowledge:methodology:review",
                                                                  "knowledge:methodology:read"},
                                      reviewer_id="rev")
    rebuilt = env.build_map(request_key="question-grade")
    assert rebuilt["strength"]["grade"]["asserted"] is False
    assert "risk of bias" not in rebuilt["strength"]["grade"]["inputs_missing"]
    assert rebuilt["strength"]["grade"]["recorded"][0]["reviewer"] == "rev"


def test_view_is_separate_from_claims_and_carries_the_boundary(built):
    _, view = built
    assert view["boundary"] == NON_ADVICE and view["strength"]["boundary"] == NON_ADVICE
    assert "not medical advice" in NON_ADVICE and "no dosing or treatment recommendation" in NON_ADVICE
    assert view["claims"]["included"] is False and "literature_claims" in view["claims"]["note"]
    assert not FORBIDDEN & set(_keys(view))
    for item in view["regulatory"]:
        if item["disclaimer"] is not None:
            assert item["disclaimer"]["text"].startswith("Do not rely on openFDA")
    events = next(r for r in view["regulatory"] if r["regulatory_kind"] == "adverse-event-summary")
    assert "not incidence" in events["count_semantics"]
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-clinical-evidence-map-v1.json").read_text())
    jsonschema.validate({k: v for k, v in view.items() if k != "freshness"}, schema)


def test_evidence_bundle_export_verifies_and_lists_gaps_as_omissions(built):
    env, view = built
    exported = EvidenceMapService(env.conn).export_bundle(NS, view["view_id"], scopes=env.scopes())
    bundle = exported["bundle"]
    result = verify_bundle(bundle)
    assert result.errors == [] and result.status == "incomplete"  # partial: gaps are declared omissions
    assert bundle["completeness"]["status"] == "partial"
    reasons = " ".join(o["reason"] for o in bundle["completeness"]["omissions"])
    assert "unregistered publication" in reasons and "unmapped term 'noetiglutide'" in reasons
    types = {o["type"] for o in bundle["objects"]}
    assert types == {"receipt", "evidence"}
    kinds = {o["payload"].get("kind") for o in bundle["objects"]}
    assert {"clinical-evidence-map", "clinical-record", "linked-publication"} <= kinds
    assert exported["boundary"] == NON_ADVICE


def test_questions_are_validated_and_maps_are_idempotent_per_request_key(built):
    env, view = built
    with pytest.raises(ClinicalRecordError):
        validate_question({"condition": "x"})
    with pytest.raises(ClinicalRecordError):
        validate_question({**QUESTION, "dose": "1"})
    with pytest.raises(ClinicalRecordError) as caught:
        env.build_map(question={**QUESTION, "intervention": "metformin"})
    assert caught.value.code == "idempotency_conflict"
    inspected = EvidenceMapService(env.conn).inspect(NS, view["view_id"], scopes=env.scopes())
    assert inspected["freshness"]["current"] is True
