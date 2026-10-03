"""AI08 (#2785) and AI09 (#2790), track #2742: records as of a date side by side, and revision and licence history."""

from __future__ import annotations

import json

import pytest

from src.kb.ai_models_identity import AiModelsIdentity
from src.kb.ai_models_queries import AiModelsQueries
from src.kb.ai_models_records import AiModelsError, excluded_paths, personal_data_paths
from tests.unit import ai_models_harness as h

A, B, C, D, E = (h.SHAS[k] for k in "ABCDE")


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    h.load_spdx(conn)
    identity = AiModelsIdentity(conn)
    identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    for match in identity.matches(h.NS, scopes=h.SCOPES):
        if match["method"] == "stated-repository-id" and match["pair_kind"] == "epoch-hub-model":
            identity.review(h.NS, match["match_id"], "accept", "row links the repository", principal_id="bob",
                            scopes=h.SCOPES)
        if "stated-openml-id" in {m["method"] for m in match["evidence"]["methods"]}:  # Hub corpus and 990061
            identity.review(h.NS, match["match_id"], "accept", "card states the OpenML id", principal_id="bob",
                            scopes=h.SCOPES)
    return conn


def test_as_of_answers_select_each_sources_current_revision_side_by_side_and_cite_it(conn):
    queries = AiModelsQueries(conn)
    answer = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2096-06-01")
    assert answer["status"] == "records"
    hub, epoch = answer["side_by_side"]
    assert (hub["relation"], epoch["relation"]) == ("subject", "accepted-match")
    assert hub["revision"]["sha"] == B and hub["citation"]["revision"]["sha"] == B
    assert hub["citation"]["as_of"] == "2096-05-10T08:00:00Z" and hub["citation"]["evidence_origin"] == "fixture"
    assert epoch["citation"]["revision"]["vintage_id"] == epoch["revision"]["vintage_id"]
    assert epoch["vintage"]["release_basis"] == "page_last_updated"
    # Reported figures stay with their reporter and are never merged.
    assert [g["kind"] for g in hub["reported"]] == ["self_reported_result"]
    assert hub["reported"][0]["items"][0]["metric"]["value_text"] == "0.74"
    assert "self-reported" in hub["reported"][0]["reported_by"]
    assert epoch["reported"][0]["kind"] == "epoch_estimate" and epoch["reported"][0]["reported_by"].startswith(
        "Epoch AI")
    assert {i["estimate"]: i["value_text"] for i in epoch["reported"][0]["items"]}["training_compute"] == "3.4e21"
    assert "never merged" in answer["not_merged"]
    later = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2098-02-01")
    hub_later, epoch_later = later["side_by_side"]
    assert hub_later["revision"]["sha"] == E and hub_later["licence"]["declared"]["license_name"] == \
        "fixture-model-licence-2.0"
    assert {i["estimate"]: i["value_text"] for i in epoch_later["reported"][0]["items"]}["training_compute"] == \
        "3.9e21"
    early = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2095-06-01")
    assert early["side_by_side"][0]["revision"]["sha"] == A
    assert early["side_by_side"][1]["status"] == "no_revision_by_as_of"
    for item in (answer, later, early):
        assert personal_data_paths(item) == [] and excluded_paths(item) == []
        assert "Ada Example" not in json.dumps(item)


def test_datasets_show_openml_evaluations_with_their_reporter_and_cite_id_and_version(conn):
    queries = AiModelsQueries(conn)
    answer = queries.records_as_of(h.NS, {"openml_dataset_id": 990061}, scopes=h.READ_ONLY, as_of="2097-06-01")
    openml = answer["side_by_side"][0]
    assert openml["citation"]["revision"] == {"openml_id": 990061, "version": "1", "status": "active"}
    group = openml["reported"][0]
    assert group["kind"] == "openml_run_evaluation" and group["reported_by"].startswith("OpenML run evaluation")
    assert {i["run_id"]: i["state"] for i in group["items"]}[99000002] == "reported"
    later = queries.records_as_of(h.NS, "openml:990061", scopes=h.READ_ONLY, as_of="2098-06-01")
    assert {i["run_id"]: i["state"] for i in later["side_by_side"][0]["reported"][0]["items"]}[99000002] == \
        "not-returned"
    hub = next(e for e in later["side_by_side"] if e["source"] == "huggingface-hub")
    assert hub["relation"] == "accepted-match" and hub["revision"]["sha"] == D
    assert hub["licence"]["spdx"]["spdx_id"] == "CC-BY-4.0"
    assert openml["licence"]["spdx"]["status"] == "not_normalised"  # "CC BY 4.0" is a name, never mapped


def test_unknown_subjects_have_no_records_and_withdrawn_states_are_reported(conn):
    queries = AiModelsQueries(conn)
    nothing = queries.records_as_of(h.NS, "example-org/no-such-model", scopes=h.READ_ONLY, as_of="2097-01-01")
    assert nothing["status"] == "no_records" and nothing["side_by_side"] == []
    small = queries.records_as_of(h.NS, {"repo_id": h.SMALL, "kind": "model"}, scopes=h.READ_ONLY,
                                  as_of="2098-06-01")
    assert small["side_by_side"][0]["status"] == "withdrawn" and small["side_by_side"][0]["reported"] == []
    with pytest.raises(AiModelsError):
        queries.records_as_of(h.NS, h.MODEL, scopes={"namespace:global:read"})


def test_revision_history_lists_revisions_states_and_licence_changes_as_declared(conn):
    queries = AiModelsQueries(conn)
    history = queries.revision_history(h.NS, h.MODEL, scopes=h.READ_ONLY)
    assert [(r["sha"], r["time"]) for r in history["revisions"]] == [
        (A, "2095-03-01T10:00:00Z"), (B, "2096-05-10T08:00:00Z"), (E, "2097-08-01T09:30:00Z")]
    first, second = history["licence_changes"]
    assert first["before"]["declared"]["license"] == "apache-2.0" and first["before"]["spdx"]["spdx_id"] == \
        "Apache-2.0"
    assert first["after"]["declared"] == {"license": "other", "license_name": "fixture-model-licence-1.0",
                                          "license_link": "LICENSE", "stated_in": "card front matter"}
    assert first["after"]["spdx"]["status"] == "not_normalised"
    assert second["after"]["declared"]["license_name"] == "fixture-model-licence-2.0"
    assert all(r["citation"]["revision_id"] == r["revision_id"] for r in history["revisions"])
    assert "No licence-compliance" in history["licence_note"]
    gated = queries.revision_history(h.NS, h.SMALL, scopes=h.READ_ONLY)
    assert [r["state"] for r in gated["revisions"]] == ["published", "withdrawn"]
    assert gated["source_stated_states"][0]["state_detail"]["gated"] == "manual"
    deactivation = queries.revision_history(h.NS, "openml:990062", scopes=h.READ_ONLY)
    assert [(r["version"], r["source_status"]) for r in deactivation["revisions"]] == [
        ("2", "in_preparation"), ("2", "active")]
    removed = queries.revision_history(h.NS, "Fixture Small Model", scopes=h.READ_ONLY)
    assert [r["state"] for r in removed["revisions"]] == ["published", "removed_by_source"]
    assert removed["revisions"][0]["vintage"]["file_sha256"] != removed["revisions"][1]["vintage"]["file_sha256"]
    assert queries.revision_history(h.NS, "nothing/here", scopes=h.READ_ONLY)["status"] == "no_records"


def test_evidence_bundle_cites_every_item_with_source_revision_and_as_of(conn):
    queries = AiModelsQueries(conn)
    answer = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2096-06-01")
    bundle = queries.evidence_bundle(answer, created_at_ms=h.SECOND_RETRIEVAL)
    assert bundle["contract"] == "noesis-evidence-bundle-v1"
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    records = [o["payload"] for o in evidence if o["payload"]["kind"] == "ai-model-record-revision"]
    assert {r["source"] for r in records} == {"huggingface-hub", "epoch-ai"}
    assert all(r["revision"] and r["as_of"] and r["locator"]["revision_id"] for r in records)
    reported = [o["payload"] for o in evidence if o["payload"]["kind"] != "ai-model-record-revision"]
    assert {r["kind"] for r in reported} == {"self_reported_result", "epoch_estimate"}
    assert all(r["reported_by"] and r["as_of"] for r in reported)
    assert bundle["completeness"]["status"] == "partial"  # unverified-live evidence is an explicit omission
