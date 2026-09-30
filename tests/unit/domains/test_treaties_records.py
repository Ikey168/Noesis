"""Treaty, participant, action and statement records with immutable revisions and as-of lookup (#2590, TR02)."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from src.kb.treaties_records import (
    TreatiesError,
    TreatiesStore,
    content_hash,
    forbidden_keys,
    validate,
)
from tests.unit import treaties_harness as h

ACCESSION = "treaties:untc:action:XXVII-99:northwind-republic:accession:1"
OLDLAND = "treaties:untc:action:XXVII-99:oldland:succession:1"


@pytest.fixture
def loaded():
    conn = h.connection()
    h.load_all(conn, observed_at_ms=1_000)
    yield conn
    conn.close()


def test_every_record_follows_the_contract_and_carries_source_revision_and_as_of(loaded):
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-treaty-record-v1.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    store = TreatiesStore(loaded)
    rows = store.records(h.NS, scopes=h.READ_ONLY)
    assert {r["provider"] for r in rows} == {"untc", "eu-cellar", "coe-treaty-office"}
    for row in rows:
        assert not list(validator.iter_errors(row["record"])), row["record_key"]
        citation = row["citation"]
        assert citation["source_id"] in h.SOURCES and citation["revision_id"].startswith("treaty-rev:")
        assert citation["as_of_ms"] == 1_000 and citation["evidence_origin"] == "fixture"
        assert citation["locator"].startswith("https://")
    untc = next(r for r in rows if r["record_key"] == h.UNTC)
    assert untc["citation"]["depositary_revision"] == "2099-01-15T09:15:00"
    assert not forbidden_keys([r["record"] for r in rows])


def test_a_depositary_correction_is_a_new_revision_and_a_dropped_row_is_removed_by_source(loaded):
    store = TreatiesStore(loaded)
    result = h.apply(loaded, "untc-treaty-status", v2=True, observed_at_ms=2_000)[0]
    assert result["counts"]["revised"] >= 2 and result["counts"]["new"] >= 3
    assert result["counts"]["removed-by-source"] == 2  # Oldland's participant and succession rows
    history = store.history(h.NS, ACCESSION, scopes=h.READ_ONLY)
    assert [r["change"] for r in history] == ["new", "revised"]
    assert history[0]["record"]["fields"]["deposit_date"] == "2092-05-10"
    assert history[1]["record"]["fields"]["deposit_date"] == "2092-05-11"
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    assert history[1]["native_revision"] == "2099-06-20T10:00:00"
    removed = store.history(h.NS, OLDLAND, scopes=h.READ_ONLY)
    assert [r["change"] for r in removed] == ["new", "removed-by-source"]
    assert removed[-1]["publication_status"] == "no-longer-published"
    assert removed[-1]["record"]["fields"] == removed[0]["record"]["fields"]  # kept, never deleted
    assert OLDLAND not in {r["record_key"] for r in store.records(h.NS, scopes=h.READ_ONLY)}
    assert OLDLAND in {r["record_key"] for r in store.records(h.NS, scopes=h.READ_ONLY, include_removed=True)}
    # An unchanged action is not re-revised because the page stamp moved on.
    signature = store.history(h.NS, "treaties:untc:action:XXVII-99:exampland:signature:1", scopes=h.READ_ONLY)
    assert [r["change"] for r in signature] == ["new"]


def test_replays_are_idempotent_and_an_older_page_never_becomes_current(loaded):
    store = TreatiesStore(loaded)
    again = h.apply(loaded, "untc-treaty-status", observed_at_ms=1_500)[0]
    assert again["counts"]["new"] == again["counts"]["revised"] == again["counts"]["removed-by-source"] == 0
    h.apply(loaded, "untc-treaty-status", v2=True, observed_at_ms=2_000)
    older = h.apply(loaded, "untc-treaty-status", run_id="replay-v1", observed_at_ms=3_000)[0]
    assert older["counts"]["revised"] == 0 and older["counts"]["removed-by-source"] == 0
    assert older["counts"]["unchanged"] + older["counts"]["older-observation"] == older["records"]
    current = store.records(h.NS, scopes=h.READ_ONLY, record_keys=[ACCESSION])[0]
    assert current["record"]["fields"]["deposit_date"] == "2092-05-11"
    assert OLDLAND not in {r["record_key"] for r in store.records(h.NS, scopes=h.READ_ONLY)}


def test_as_of_lookup_by_record_time_and_by_depositary_revision(loaded):
    store = TreatiesStore(loaded)
    h.apply(loaded, "untc-treaty-status", v2=True, observed_at_ms=2_000)
    assert store.as_known_at(h.NS, ACCESSION, 1_500, scopes=h.READ_ONLY)["record"]["fields"]["deposit_date"] == \
        "2092-05-10"
    assert store.as_known_at(h.NS, ACCESSION, 2_500, scopes=h.READ_ONLY)["record"]["fields"]["deposit_date"] == \
        "2092-05-11"
    assert store.as_known_at(h.NS, ACCESSION, 500, scopes=h.READ_ONLY) is None
    assert store.as_of_depositary(h.NS, ACCESSION, "2099-03-01", scopes=h.READ_ONLY)["revision_no"] == 1
    assert store.as_of_depositary(h.NS, ACCESSION, "2099-07-01", scopes=h.READ_ONLY)["revision_no"] == 2
    assert store.as_of_depositary(h.NS, ACCESSION, "2098-12-31", scopes=h.READ_ONLY) is None


def test_minimisation_and_exclusions_are_enforced_at_write_time(loaded):
    store = TreatiesStore(loaded)
    record = copy.deepcopy(h.records("coe-treaty-office")[0])
    for bad, code in ((dict(record, fields={**record["fields"], "signatory_name": "A. Person"}),
                       "minimisation_violation"),
                      (dict(record, fields={**record["fields"], "representative": {"name": "A. Person"}}),
                       "minimisation_violation"),
                      (dict(record, minimisation=None), "minimisation_violation"),
                      (dict(record, fields={**record["fields"], "legal_effect": "binding"}), "assessment_forbidden")):
        with pytest.raises(TreatiesError) as exc:
            store.project(h.NS, [bad], run_id="bad", source_id="coe-treaty-office")
        assert exc.value.code == code
    statement = next(r for r in h.records("coe-treaty-office") if r["record_kind"] == "treaty-statement")
    leaked = dict(statement, fields={**statement["fields"], "text_verbatim": "Contact: Tel. +99 123 456 789 or desk@example.org"})
    with pytest.raises(TreatiesError) as exc:
        validate(leaked)
    assert exc.value.code == "minimisation_violation"
    participant = next(r for r in h.records("coe-treaty-office") if r["record_kind"] == "participant")
    with pytest.raises(TreatiesError):
        validate(dict(participant, fields={**participant["fields"], "participant_type": "person"}))
    with pytest.raises(TreatiesError) as exc:
        store.records(h.NS, scopes={"knowledge:legal:read"})
    assert exc.value.code == "unauthorized"
    assert content_hash(record) == content_hash(dict(record, native_revision="2100-01-01"))
