"""AI02 (#2751, track #2742): AI model and dataset registry records with revisions, observations and as-of lookup."""

from __future__ import annotations

import copy
import json

import pytest

from src.kb.ai_models_records import (
    CONTRACT,
    AiModelsError,
    normalise_licence,
    personal_data_paths,
    validate,
)
from src.kb.ai_models_store import AiModelsStore, citation
from src.kb.oss_ecosystem_store import OssEcosystemStore
from tests.unit import ai_models_harness as h

A, B, C, D, E = (h.SHAS[k] for k in "ABCDE")


def _hub_revision(**changes):
    statement = next(s for s in h.statements("hub") if s["record_type"] == "hub_repository_revision"
                     and s.get("sha") == A)
    return {**copy.deepcopy(statement), **changes}


@pytest.mark.parametrize("field,where", [
    ("author", "top"), ("authors", "declared"), ("creator", "top"), ("contributor", "top"), ("uploader", "top"),
    ("uploader_name", "nested"), ("Authors", "top"), ("commit_author", "nested"), ("username", "nested"),
])
def test_person_fields_are_refused_at_parse_time_reusing_check_no_people(field, where):
    statement = _hub_revision()
    if where == "top":
        statement[field] = "Ada Example"
    elif where == "declared":
        statement["declared"][field] = ["Ada Example"]
    else:
        statement["card"]["front_matter"] = {"license": "mit"}
        statement["files"][field] = "ada"
    with pytest.raises(AiModelsError) as caught:
        validate(statement)
    assert caught.value.code == "personal_field"
    for epoch_row in ({"record_type": "epoch_model_revision", "source": "epoch-ai", "model": "Fixture Model",
                       "reported_by": "Epoch AI", "Authors": "Ada Example"},
                      {"record_type": "openml_dataset_revision", "source": "openml", "dataset_id": 1, "name": "x",
                       "status": "active", "creator": ["Ada Example"]}):
        with pytest.raises(AiModelsError, match="identity"):
            validate(epoch_row)


@pytest.mark.parametrize("field", ["downloads", "likes", "trendingScore", "card_body", "discussions",
                                   "commit_history", "ranking", "safety_verdict"])
def test_excluded_counts_bodies_and_verdicts_are_refused(field):
    statement = _hub_revision()
    statement[field] = 1
    with pytest.raises(AiModelsError) as caught:
        validate(statement)
    assert caught.value.code == "excluded_field"


def test_parsed_statements_carry_no_person_field_popularity_count_or_card_body():
    every = [s for name in h.SOURCES for s in h.statements(name)]
    assert every and all(validate(s) for s in every)
    text = json.dumps(every)
    for leaked in ("Ada Example", "Bob Example", "Cy Example", "Dee Example", "990001", "123456", "789",
                   "authored prose", "never stored either", "uploader", "downloads", "likes"):
        assert leaked not in text, leaked
    assert personal_data_paths(every) == []
    card = next(s for s in every if s.get("sha") == A)["card"]
    assert card["status"] == "stated" and card["body"].startswith("cited by revision URL and digest")
    assert len(card["sha256"]) == len(card["body_sha256"]) == 64
    assert card["url"] == f"https://huggingface.co/{h.MODEL}/resolve/{A}/README.md"
    assert set(card["front_matter"]) <= {"license", "library_name", "pipeline_tag", "language", "datasets",
                                         "base_model", "tags"}


def test_hub_revisions_are_keyed_by_sha_a_pinned_sha_is_immutable_and_a_new_sha_is_a_new_revision():
    conn = h.connection()
    h.apply(conn, "hub", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = AiModelsStore(conn)
    model = h.record(conn, "hub-model", h.MODEL)
    assert [r["revision_key"] for r in store.revision_rows(h.NS, model["record_id"])] == [A, B]
    again = h.apply(conn, "hub", retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert {r["status"] for r in again} == {"unchanged"}
    assert len(store.revision_rows(h.NS, model["record_id"])) == 2
    # Other content under a stored sha is refused and the stored revision kept.
    header = {"provider": "huggingface-hub", "format": "hf-hub-api-json", "unit_key": "hub:model:x",
              "statement_count": 1, "evidence_origin": "fixture"}
    tampered = _hub_revision(declared={"licence": {"license": "mit", "stated_in": "card front matter"}})
    with pytest.raises(AiModelsError) as caught:
        store.apply_unit(h.NS, header, [tampered], source_id="huggingface-hub", run_id="run:x", principal_id="svc",
                         scopes=h.SCOPES, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert caught.value.code == "immutable_revision"
    h.apply(conn, "hub", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    chain = store.revision_rows(h.NS, model["record_id"])
    assert [r["revision_key"] for r in chain] == [A, B, E]
    assert [r["source_time"] for r in chain] == ["2095-03-01T10:00:00Z", "2096-05-10T08:00:00Z",
                                                 "2097-08-01T09:30:00Z"]
    assert chain[1]["changes"]["licence_change"]["declared_before"] == "apache-2.0"
    assert chain[1]["changes"]["licence_change"]["declared_after"] == "other"
    assert chain[2]["changes"]["licence_change"]["after"]["license_name"] == "fixture-model-licence-2.0"


def test_as_of_lookup_selects_the_revision_current_at_the_date():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = AiModelsStore(conn)
    model = h.record(conn, "hub-model", h.MODEL)
    for day, sha in (("2095-06-01", A), ("2096-05-11", B), ("2097-12-31", E)):
        revision, reason = store.select_revision(h.NS, model["record_id"], as_of_ms=h.day_ms(day))
        assert revision["revision_key"] == sha and reason is None
    assert store.select_revision(h.NS, model["record_id"], as_of_ms=h.day_ms("2094-01-01")) == (
        None, "no_revision_by_as_of")
    revision, _ = store.select_revision(h.NS, model["record_id"], as_of_ms=h.day_ms("2096-06-01"))
    cited = citation(model, revision)
    assert cited["revision"]["sha"] == B and cited["as_of"] == "2096-05-10T08:00:00Z"
    assert cited["contract"] == CONTRACT and cited["live_verification"] == "unverified-live"
    assert cited["evidence_origin"] == "fixture" and cited["time_basis"] == "last_modified"


def test_gated_and_404_repositories_get_source_stated_revisions_and_keep_their_history():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = AiModelsStore(conn)
    small = h.record(conn, "hub-model", h.SMALL)
    chain = store.revision_rows(h.NS, small["record_id"])
    assert [r["state"] for r in chain] == ["published", "withdrawn"]
    assert chain[1]["statement"]["state_detail"]["gated"] == "manual"
    assert chain[1]["time_basis"] == "retrieval_time" and chain[1]["revision_key"] != C
    assert store.select_revision(h.NS, small["record_id"], as_of_ms=h.day_ms("2097-06-01"))[0]["revision_key"] == C
    # A 404 is a removed_by_source revision; a re-publication at a stored sha is a new revision again.
    pages = [p for p in h.pages("hub", revision=True)]
    pages = [{**p, "status": 404, "body": None} if p["request"].endswith("/api/datasets/" + h.CORPUS) else p
             for p in pages]
    from src.ingestion.ai_models_sources import fixture_transport

    projector_records = [r for page in h.fetch("hub", transport=fixture_transport(pages)) for r in page]
    removed = [r["ai_statement"] for r in projector_records if r["ai_statement"].get("repo_id") == h.CORPUS]
    assert removed == [{"record_type": "hub_repository_revision", "source": "huggingface-hub", "kind": "dataset",
                        "repo_id": h.CORPUS, "organisation": "example-org", "state": "removed_by_source",
                        "state_detail": {"http_status": 404, "basis": "the repository answers 404; earlier "
                                                                      "revisions stay in its history"},
                        "url": f"https://huggingface.co/datasets/{h.CORPUS}"}]


def test_openml_versions_status_changes_and_immutable_evaluations():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = AiModelsStore(conn)
    v2 = h.record(conn, "openml-dataset", "990062")
    chain = store.revision_rows(h.NS, v2["record_id"])
    assert [r["statement"]["status"] for r in chain] == ["in_preparation", "active"]
    assert [r["time_basis"] for r in chain] == ["upload_date", "retrieval_time"]
    assert chain[0]["source_time"] == "2096-01-05T09:00:00Z"
    v1 = h.record(conn, "openml-dataset", "990061")
    assert v1["revision_count"] == 1 and v1["record_id"] != v2["record_id"]
    first = store.revision_rows(h.NS, v1["record_id"])[0]["statement"]
    assert first["licence"] == {"licence": "CC BY 4.0", "stated_in": "OpenML licence field"}
    assert "description" not in first and len(first["description_sha256"]) == 64
    task = h.record(conn, "openml-task", "9900001")
    observations = {o["statement"]["run_id"]: o for o in store.observations(h.NS, task["record_id"])}
    assert observations[99000002]["state"] == "not-returned"
    assert [s["state"] for s in observations[99000002]["state_history"]] == ["reported", "not-returned"]
    assert observations[99000004]["state"] == "reported"
    assert {o["reported_by"] for o in observations.values()} == {
        "OpenML run evaluation, computed by OpenML for a run uploaded to OpenML"}
    as_of_first = {o["statement"]["run_id"]: o["state"] for o in store.observations(
        h.NS, task["record_id"], as_of_ms=h.day_ms("2097-06-01"))}
    # By source time: run 99000004 (uploaded 2097-03-03) existed then; run 99000002 was still listed.
    assert as_of_first == {99000001: "reported", 99000002: "reported", 99000003: "reported", 99000004: "reported"}
    changed = next(s for s in h.statements("openml") if s.get("run_id") == 99000001)
    header = {"provider": "openml", "format": "openml-json-v1", "unit_key": "openml:task:9900001",
              "statement_count": 2, "evidence_origin": "fixture"}
    task_statement = next(s for s in h.statements("openml") if s["record_type"] == "openml_task_revision")
    with pytest.raises(AiModelsError) as caught:
        store.apply_unit(h.NS, header, [task_statement, {**changed, "value_text": "0.99"}], source_id="openml",
                         run_id="run:x", principal_id="svc", scopes=h.SCOPES)
    assert caught.value.code == "immutable_observation"


def test_epoch_rows_are_vintaged_revised_and_removed_by_source_with_confidence_labels():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = AiModelsStore(conn)
    row = h.record(conn, "epoch-model", "Fixture Model")
    chain = store.revision_rows(h.NS, row["record_id"])
    assert [r["statement"]["estimates"]["training_compute"]["value_text"] for r in chain] == ["3.4e21", "3.9e21"]
    assert [r["statement"]["estimates"]["training_compute"]["confidence"] for r in chain] == ["Likely", "Confident"]
    assert [r["time_basis"] for r in chain] == ["page_last_updated", "retrieval_time"]
    first, second = (store.vintage(h.NS, r["vintage_id"]) for r in chain)
    assert first["release_at"] == "2096-01-15T00:00:00Z" and second["release_basis"] == "retrieval_time"
    assert first["file_sha256"] != second["file_sha256"]
    assert chain[1]["changes"]["changed_fields"] == ["confidence", "estimates"]
    small = store.revision_rows(h.NS, h.record(conn, "epoch-model", "Fixture Small Model")["record_id"])
    assert [r["state"] for r in small] == ["published", "removed_by_source"]
    assert h.record(conn, "epoch-model", "Fixture Small Model")["current_state"] == "removed_by_source"
    assert store.find(h.NS, "epoch-model", "Other Fixture Model") is None  # undeclared rows are not stored
    assert "parameters" in small[0]["statement"]["estimates"]
    assert "training_compute" not in small[0]["statement"]["estimates"]  # never inferred where Epoch states none


def test_self_reported_results_are_observations_labelled_and_pinned_to_their_sha():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = AiModelsStore(conn)
    model = h.record(conn, "hub-model", h.MODEL)
    results = store.observations(h.NS, model["record_id"])
    assert {o["statement"]["sha"]: o["statement"]["metric"]["value_text"] for o in results} == {
        A: "0.71", B: "0.74", E: "0.74"}
    assert {o["kind"] for o in results} == {"self_reported_result"}
    assert all("self-reported" in o["reported_by"] for o in results)
    pinned = {r["revision_key"]: r["revision_id"] for r in store.revision_rows(h.NS, model["record_id"])}
    assert all(o["revision_id"] == pinned[o["statement"]["sha"]] for o in results)
    assert [o["statement"]["sha"] for o in store.observations(h.NS, model["record_id"], sha=B)] == [B]


def test_declared_licences_normalise_to_spdx_only_on_an_exact_id_match():
    conn = h.connection()
    assert normalise_licence({"license": "apache-2.0"}, None)["status"] == "no_spdx_list"
    h.load_spdx(conn)
    spdx = OssEcosystemStore(conn, initialize=False).spdx_list(h.NS)
    exact = normalise_licence({"license": "apache-2.0", "stated_in": "card front matter"}, spdx)
    assert exact == {"status": "normalised", "declared": "apache-2.0", "spdx_id": "Apache-2.0", "deprecated": False,
                     "rule": "exact-spdx-id (ids are case-insensitive)", "spdx_list_version": "3.99"}
    for declared in ("other", "openrail", "MIT License", "Apache-2.0 OR MIT", "fixture-model-licence-1.0",
                     "CC BY 4.0"):
        result = normalise_licence({"license": declared}, spdx)
        assert result["status"] == "not_normalised" and result["declared"] == declared, declared
    assert normalise_licence({"licence": "Public"}, spdx)["status"] == "not_normalised"
    assert normalise_licence({}, spdx)["status"] == "none_declared"


def test_a_failed_unit_writes_nothing_and_marks_nothing_removed():
    conn = h.connection()
    h.load_all(conn)
    store = AiModelsStore(conn)
    before = store.generation(h.NS)
    store.record_failure(h.NS, "huggingface-hub", code="rate_limited", run_id="run:fail", source_id="huggingface-hub",
                         scopes=h.SCOPES)
    assert store.generation(h.NS) == before
    assert store.provider_state(h.NS, "huggingface-hub")["stale"] is True
    with pytest.raises(AiModelsError) as caught:
        store.apply_unit(h.NS, {"provider": "openml", "format": "openml-json-v1", "statement_count": 1},
                         [{"record_type": "openml_dataset_revision", "source": "openml", "dataset_id": 3, "name": "x",
                           "status": "active", "uploader": "990001"}], source_id="openml", run_id="r",
                         principal_id="svc", scopes=h.SCOPES)
    assert caught.value.code == "personal_field" and store.generation(h.NS) == before
    with pytest.raises(AiModelsError):
        store.apply_unit(h.NS, {"provider": "openml", "format": "openml-json-v1", "statement_count": 0}, [],
                         source_id="openml", run_id="r", principal_id="svc", scopes=h.READ_ONLY)
