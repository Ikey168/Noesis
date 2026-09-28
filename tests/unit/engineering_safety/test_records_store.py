"""Engineering Safety records and store: round trip, revisions, immutability and refusals (ES02, #2063)."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.kb.engineering_safety_records import (
    CONTRACT,
    EngineeringSafetyError,
    content_view,
    register_schemas,
    schema_definitions,
    subject_key,
    validate_statement,
)
from src.kb.engineering_safety_store import EngineeringSafetyStore, inspect_record
from tests.unit.engineering_safety import harness as h


def ad(**changes):
    statement = {
        "contract": CONTRACT, "provider": "faa-ad", "record_kind": "directive", "native_id": "2026-04-12",
        "authority": {"code": "us-faa", "declared": "Federal Aviation Administration"},
        "identifiers": {"fr_document_number": "2026-04512"}, "revision_date": "2026-03-04",
        "revision_date_basis": "publication", "effective_date": "2026-04-08", "title": "Airworthiness Directives",
        "applicability": [{"text": "Models EX-100 airplanes", "parse_state": "parsed",
                           "parsed": {"models": ["EX-100"]}, "locator": {"paragraph": "(c)"}}],
        "statements": [{"kind": "required_action", "label": "(g)", "text": "Inspect the rod end.",
                        "locator": {"paragraph": "(g)", "span": [10, 30]}}],
        "subjects": [{"kind": "aircraft_model", "fields": {"model": "EX-100"}, "source_string": "EX-100",
                      "locator": {"paragraph": "(c)"}}],
    }
    statement.update(changes)
    return statement


@pytest.fixture()
def store():
    conn = duckdb.connect(":memory:")
    clock = iter(range(1000, 10**9, 1000))
    return EngineeringSafetyStore(conn, now=lambda: next(clock))


def test_round_trip_keeps_native_identifiers_verbatim_statements_and_locators(store):
    created = store.apply(h.NS, ad())
    assert created["status"] == "created"
    record = inspect_record(store.conn, h.NS, "faa-ad:2026-04-12", scopes=h.READ)
    assert record["native_id"] == "2026-04-12" and record["authority"] == "us-faa"
    revision = record["revision"]
    assert revision["effective_date"] == "2026-04-08" and revision["revision_date"] == "2026-03-04"
    assert revision["statements"][0]["text"] == "Inspect the rod end." and revision["statements"][0]["quoted"]
    assert revision["statements"][0]["locator"] == {"paragraph": "(g)", "span": [10, 30]}
    assert revision["subjects"][0]["subject_key"] == "aircraft-model:ex100"
    assert "None" not in json.dumps(record)


def test_revisions_append_prior_ones_stay_immutable_and_replay_adds_nothing(store):
    first = store.apply(h.NS, ad())
    assert store.apply(h.NS, ad())["status"] == "unchanged"
    corrected = ad(revision_date="2026-03-20", revision_label="correction",
                   statements=[{"kind": "required_action", "label": "(g)", "text": "Inspect the rod end within 50 h.",
                                "locator": {"paragraph": "(g)"}}])
    second = store.apply(h.NS, corrected)
    assert second["status"] == "revised" and second["record_id"] == first["record_id"]
    old = store.parts(h.NS, first["revision_id"])
    assert old["statements"][0]["text"] == "Inspect the rod end."  # never overwritten
    assert store.current_revision_id(h.NS, first["record_id"]) == second["revision_id"]
    # Late older content: history, never current, and never a second copy.
    assert store.apply(h.NS, ad())["status"] == "unchanged"
    late = ad(revision_date="2026-03-01", title="an older publication delivered late")
    assert store.apply(h.NS, late)["status"] == "history"
    assert store.current_revision_id(h.NS, first["record_id"]) == second["revision_id"]
    # Returning to earlier content with a newer date is a reversion revision, not a no-op.
    reverted = store.apply(h.NS, ad(revision_date="2026-03-20"))
    assert reverted["status"] in {"reverted", "revised"}
    assert store.current_revision_id(h.NS, first["record_id"]) == reverted["revision_id"]


def test_the_record_key_does_not_depend_on_arrival_order(store):
    later = store.apply(h.NS, ad(revision_date="2026-03-20", title="corrected"))
    earlier = store.apply(h.NS, ad())
    assert later["record_id"] == earlier["record_id"] and earlier["status"] == "history"
    assert store.current_revision_id(h.NS, later["record_id"]) == later["revision_id"]


def test_a_final_report_ranks_above_a_preliminary_one_without_deleting_it(store):
    base = {"contract": CONTRACT, "provider": "ntsb", "record_kind": "investigation", "native_id": "ERA26FA101",
            "authority": {"code": "us-ntsb", "declared": "NTSB"}}
    final = store.apply(h.NS, {**base, "report_status": "final", "revision_date": "2026-08-14",
                               "statements": [{"kind": "probable_cause", "text": "Fatigue fracture.",
                                               "locator": {"json_pointer": "/ProbableCause"}}]})
    preliminary = store.apply(h.NS, {**base, "report_status": "preliminary", "revision_date": "2026-09-01"})
    assert preliminary["status"] == "history"  # a later-dated preliminary never replaces the final report
    assert store.current_revision_id(h.NS, final["record_id"]) == final["revision_id"]
    assert len(store.revisions(h.NS, final["record_id"])) == 2


def test_an_unchanged_row_in_a_newer_file_vintage_adds_nothing(store):
    row = {"contract": CONTRACT, "provider": "phmsa", "record_kind": "occurrence", "native_id": "20260045",
           "authority": {"code": "us-phmsa", "declared": "PHMSA"}, "revision_date": "2026-06-01",
           "revision_date_basis": "file-vintage",
           "statements": [{"kind": "cause_category", "text": "CORROSION FAILURE", "locator": {"row": 2}}]}
    first = store.apply(h.NS, row)
    moved = copy.deepcopy(row)
    moved["revision_date"] = "2026-09-01"
    moved["statements"][0]["locator"] = {"row": 7}
    assert content_view(moved) == content_view(row)
    assert store.apply(h.NS, moved)["status"] == "unchanged"
    changed = copy.deepcopy(moved)
    changed["statements"][0]["text"] = "EQUIPMENT FAILURE"
    second = store.apply(h.NS, changed)
    assert second["status"] == "revised" and second["record_id"] == first["record_id"]


@pytest.mark.parametrize("change", [
    {"verdict": "unsafe"}, {"risk_score": 0.4}, {"subjects": [{"kind": "vehicle", "source_string": "x",
                                                              "fields": {"safe": False}}]},
    {"record_kind": "complaint"}, {"report_status": "draft"}, {"revision_date": "08/04/2026"},
    {"statements": [{"kind": "required_action", "text": "no locator"}]},
    {"applicability": [{"text": "x", "parse_state": "guessed", "locator": {"p": 1}}]},
])
def test_verdicts_unknown_kinds_and_unlocated_statements_are_refused(store, change):
    with pytest.raises(EngineeringSafetyError) as refused:
        store.apply(h.NS, ad(**change))
    assert refused.value.code == "invalid_record"


def test_absent_values_are_dropped_never_written_as_strings():
    value = validate_statement(ad(title=None, identifiers={"docket": None, "fr_document_number": "2026-04512"}))
    assert "title" not in value and value["identifiers"] == {"fr_document_number": "2026-04512"}


def test_subject_keys_per_domain():
    assert subject_key("aircraft", {"make": "Examplar", "model": "EX 100"}) == "aircraft-model:ex100"
    assert subject_key("vehicle", {"make": "VELOMARK", "model": "CITYRUNNER", "model_year": "2025"}) == \
        "vehicle:velomark:cityrunner:2025"
    assert subject_key("component", {"part_number": "P/N-12", "manufacturer": "Acme Inc"}) == "component:acme:pn12"
    assert subject_key("pipeline_operator", {"operator_id": "39999"}) == "pipeline-operator:39999"
    assert subject_key("facility", {"name": "Plant A", "address": "Harbor City, TX"}).startswith("facility:plant a:")
    assert subject_key("vehicle", {"make": "VELOMARK"}) is None


def test_every_read_is_not_ready_before_a_source_ran():
    conn = duckdb.connect(":memory:")
    with pytest.raises(EngineeringSafetyError) as caught:
        inspect_record(conn, h.NS, "faa-ad:2026-04-12", scopes=h.READ)
    assert caught.value.code == "not_ready"


def test_contracts_are_registered_in_the_schema_registry_idempotently():
    conn = duckdb.connect(":memory:")
    scopes = {"knowledge:schema:register", "knowledge:schema:read"}
    first = register_schemas(conn, principal_id="svc", scopes=scopes)
    again = register_schemas(conn, principal_id="svc", scopes=scopes)
    assert len(first) == 2 == len(again)
    assert set(schema_definitions()) == {"noesis-engineering-safety-record", "noesis-engineering-safety-dossier"}
    record_schema = schema_definitions()["noesis-engineering-safety-record"]
    from jsonschema import Draft7Validator

    Draft7Validator.check_schema(record_schema)
    assert not list(Draft7Validator(record_schema).iter_errors(validate_statement(ad())))
