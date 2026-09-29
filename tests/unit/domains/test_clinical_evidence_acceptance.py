"""Offline Clinical Evidence acceptance: clinical question to cited evidence map (H13, tracker #1847).

The source-pack runtime, native adapters, projector, Science connectors'
parsers, paper families, Crossref notices, methodology provenance, ontology,
evidence bundles and subscriptions run for real over *authored* fixtures
(tests/fixtures/clinical/README.md); every socket is refused. This is offline
evidence only and never live coverage. One test per acceptance row.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.clinical_providers import LIVE_VERIFICATION
from src.kb.clinical_evidence import NON_ADVICE, EvidenceMapService
from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore, digest
from tests.unit.clinical.harness import NS, QUESTION, Env


@pytest.fixture(scope="module", autouse=True)
def no_network():
    original = socket.socket.connect

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    socket.socket.connect = refuse
    yield
    socket.socket.connect = original


@pytest.fixture(scope="module")
def journey():
    env = Env()
    receipt, aligned, linked = env.journey()
    view = env.build_map()
    return {"env": env, "receipt": receipt, "aligned": aligned, "linked": linked, "view": view,
            "trials": {t["identifier"]: t for t in view["trials"]}}


def test_row_question_intake(journey):
    view = journey["view"]
    assert view["question"] == {**QUESTION, "outcomes": ["HbA1c"]}
    condition = view["expansion"]["condition"]
    assert condition["mapped"] and condition["mesh_ids"] == ["D003924"] and condition["steps"]
    assert view["expansion"]["intervention"]["matched_labels"] == ["noetiglutide"]


def test_row_acquisition_from_fixtures_without_network(journey):
    receipt = journey["receipt"]
    assert receipt["status"] == "complete"
    assert {s["source_id"]: s["status"] for s in receipt["sources"]} == {
        "ctgov-question-search": "complete", "ctgov-trial-history": "complete", "ctis-trials": "complete",
        "ema-medicines": "complete", "euctr-trials": "complete", "openfda-products": "complete"}
    assert all(s["output_hash"] and s["counts"]["pages"] >= 1 for s in receipt["sources"])
    env = journey["env"]
    executions = {r[0] for r in env.conn.execute("SELECT last_execution FROM clinical_provider_state").fetchall()}
    assert executions <= {"injected", "user-supplied"}


def test_row_cross_registry_linking(journey):
    t1, t3 = journey["trials"]["NCT09000001"], journey["trials"]["NCT09000003"]
    assert t1["cross_registry"][0]["identifier"] == "2015-900001-10" and t1["cross_registry"][0]["merged"] is False
    assert t3["cross_registry"][0]["identifier"] == "2023-509001-12-00"
    assert t1["record_id"] != journey["trials"]["2015-900001-10"]["record_id"]
    evidence = t3["cross_registry"][0]["link_evidence"]
    assert {e["evidence"]["declared_as"] for e in evidence} == {"eu-ct", "nct"}


def test_row_publication_linking(journey):
    env, t1 = journey["env"], journey["trials"]["NCT09000001"]
    [publication] = t1["publications"]
    assert publication["document_id"] == env.documents["pubmed:99000001"]
    assert set(publication["evidence_kinds"]) == {"registry-declared-reference", "secondary-source-identifier",
                                                  "paper-family"}
    assert journey["linked"]["declared_not_harvested"] == []


def test_row_methodology_extraction(journey):
    t1 = journey["trials"]["NCT09000001"]
    design = t1["design"]
    assert (design["phase"], design["allocation"], design["masking"]) == (["phase-3"], "randomized", "quadruple")
    assert (design["sample_size_planned"], design["sample_size_actual"]) == (600, 612)
    assert design["preregistration"]["prospective"] is True
    assert design["methodology"]["study_id"].startswith("study:")
    env = journey["env"]
    statements = env.conn.execute("SELECT kind, locator_json FROM methodology_statements").fetchall()
    assert ("primary-outcome", ) == statements[0][:1] and json.loads(statements[0][1])["passage"] >= 0


def test_row_outcome_switching_detection(journey):
    findings = journey["trials"]["NCT09000001"]["outcome_switching"]
    assert [f["kind"] for f in findings] == ["primary-retimed", "publication-retimed"]
    assert findings[0]["before"]["registry_version"] != findings[0]["after"]["registry_version"]
    assert all(f["finding"] is True and "not a judgement" in f["note"] for f in findings)


def test_row_strength_view(journey):
    strength = journey["view"]["strength"]
    assert strength["summary"]["rule"] == "S2" and strength["score"] is None
    assert strength["grade"]["asserted"] is False and strength["grade"]["inputs_missing"]
    assert journey["view"]["boundary"] == NON_ADVICE
    assert journey["view"]["claims"]["included"] is False


def test_row_evidence_bundle_export(journey):
    env, view = journey["env"], journey["view"]
    exported = EvidenceMapService(env.conn).export_bundle(NS, view["view_id"], scopes=env.scopes())
    result = verify_bundle(exported["bundle"])
    assert result.errors == []
    root = next(o for o in exported["bundle"]["objects"] if o["id"] in exported["bundle"]["roots"])
    assert root["payload"]["kind"] == "clinical-evidence-map" and root["payload"]["boundary"] == NON_ADVICE


def test_row_retracted_publications(journey):
    t2 = journey["trials"]["NCT09000002"]
    assert t2["retracted"] is True and t2["retractions"][0]["notice_type"] == "retraction"
    retracted = [p for p in t2["publications"] if p["notices"]]
    assert retracted and retracted[0]["notices"][0]["target_doi"] == "10.5555/noetic2.2021"
    assert journey["view"]["strength"]["counts"]["retracted"] == 1


def test_row_results_never_posted(journey):
    t2 = journey["trials"]["NCT09000002"]
    assert t2["status"]["normalized"] == "terminated" and t2["results"]["state"] == "not-posted"
    assert t2["publications"]  # a publication never stands in for posted results
    assert "no results posting is recorded" in t2["results"]["note"]


def test_row_conflicting_registry_versions(journey):
    env = journey["env"]
    t3 = journey["trials"]["NCT09000003"]
    fields = {d["field"] for d in t3["cross_registry"][0]["disagreements"]}
    assert {"status", "design.enrollment_planned"} <= fields
    env.web.set("/api/v2/studies/NCT09000001", fixture="ctgov_NCT09000001_conflicting_current.json")
    env.acquire("conflict", ["ctgov-trial-history"])
    store = ClinicalRecordStore(env.conn)
    conflicts = store.history(NS, journey["trials"]["NCT09000001"]["record_id"], scopes=env.scopes())[
        "version_conflicts"]
    assert conflicts and conflicts[0]["version_key"] == "registry-history:3"
    assert conflicts[0]["conflicting_record"]["sponsor"]["lead"] == "Fixture Therapeutics AG"


def test_row_unmapped_terms(journey):
    gaps = journey["view"]["coverage_gaps"]
    assert gaps["unmapped_terms"] == [{"question_field": "intervention", "term": "noetiglutide", "reason": (
        "no crosswalk mapping to MeSH; matched through registry-native labels only")}]
    assert {u["label"] for u in journey["aligned"]["unmapped"]} == {"Diabetes mellitus type 2 (adolescents)",
                                                                    "NOETIGLUTIDE"}
    assert gaps["unregistered_publications"][0]["identifiers"]["pmid"] == "99000003"


def test_row_namespace_isolation(journey):
    env, view = journey["env"], journey["view"]
    other = {"knowledge:clinical:read", "knowledge:clinical:write", "namespace:other:read", "namespace:other:write"}
    with pytest.raises(ClinicalRecordError):
        EvidenceMapService(env.conn).inspect(NS, view["view_id"], scopes=other)
    with pytest.raises(ClinicalRecordError):
        EvidenceMapService(env.conn).inspect("other", view["view_id"], scopes=other)
    empty = EvidenceMapService(env.conn).build("other", QUESTION, principal_id="mallory", scopes=other,
                                               request_key="q", documents=[])
    assert empty["trials"] == [] and empty["strength"]["summary"]["rule"] == "S5"


def test_row_offline_evidence_is_reported_separately_from_live(journey):
    kind = journey["view"]["evidence_kind"]
    assert kind["offline_only"] is True and kind["live_providers"] == []
    assert all(v["status"] != "verified-live" for v in LIVE_VERIFICATION.values())


def test_row_the_journey_is_reproducible(journey):
    replay = Env()
    replay.journey()
    again = replay.build_map()

    def essence(view):
        return digest({"trials": [(t["identifier"], t["results"]["state"], t["retracted"],
                                   [f["kind"] for f in t["outcome_switching"]], t["design"]["category"]["rule_id"])
                                  for t in view["trials"]],
                       "summary": view["strength"]["summary"]["rule"], "gaps": view["coverage_gaps"]})
    assert essence(again) == essence(journey["view"])
