"""Fact-check and publisher records: revision chains, absences as revisions, as-of lookup and write-time guards (#2670).
"""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.fact_checks_records import (
    FactCheckError,
    FactCheckStore,
    day_ms,
    forbidden_keys,
)
from tests.unit import fact_checks_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-fact-check-record-v2.json").read_text())


def loaded(*, v2: bool = True):
    conn = h.connection()
    h.load_all(conn)
    if v2:
        h.load_all(conn, version="v2")
    return conn, FactCheckStore(conn)


def key_for(store, claim_text, publisher, source_id=None):
    return next(v["record_key"] for v in store.records(h.NS, scopes=h.SCOPES, kinds=["fact-check"])
                if v["record"]["fields"]["claims"][0]["claim_text"] == claim_text
                and v["publisher_key"].endswith(publisher) and (source_id is None or v["source_id"] == source_id))


def test_every_stored_record_conforms_to_the_contract_schema():
    _, store = loaded()
    views = store.records(h.NS, scopes=h.SCOPES)
    assert views
    validator = Draft202012Validator(SCHEMA)
    for view in views:
        assert not list(validator.iter_errors(view["record"])), view["record_key"]
        assert forbidden_keys(view["record"]) == []
        citation = view["citation"]
        assert citation["source_id"] and citation["revision_id"] and citation["observed_at"]


def test_a_publisher_update_is_a_new_revision_and_the_old_one_stays():
    _, store = loaded()
    key = key_for(store, h.C1, "factdesk.example", h.GOOGLE)
    chain = store.history(h.NS, key, scopes=h.SCOPES, source_id=h.GOOGLE)
    assert [(v["revision_no"], v["change"]) for v in chain] == [(1, "new"), (2, "revised")]
    assert chain[1]["previous_revision_id"] == chain[0]["revision_id"]
    assert [v["record"]["fields"]["claims"][0]["rating"]["text"] for v in chain] == [
        "False", "False (updated with the company's statement)"]
    assert [v["effective_on"] for v in chain] == ["2099-06-02", "2099-06-09"]
    # the same review from the Data Commons release keeps its own provenance chain
    assert [v["change"] for v in store.history(h.NS, key, scopes=h.SCOPES, source_id=h.DATACOMMONS)] == ["new"]


def test_removals_are_revisions_and_nothing_is_deleted():
    _, store = loaded()
    solar = key_for(store, h.C3, "contoso-check.example")
    chain = store.history(h.NS, solar, scopes=h.SCOPES)
    assert [(v["change"], v["status"]) for v in chain] == [("new", "published"), ("absent", "absent-from-release")]
    assert chain[1]["native_revision"] == "2099-08-01T00:00:00Z"
    assert chain[1]["record"]["fields"]["claims"] == chain[0]["record"]["fields"]["claims"]
    contoso = store.history(h.NS, "fact-check:publisher:contoso-check.example", scopes=h.SCOPES)
    assert [v["status"] for v in contoso] == ["published", "absent-from-listing"]
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[solar], include_absent=False) == []


def test_as_of_lookup_reads_publisher_status_on_a_day_as_known_at_a_time():
    _, store = loaded()
    northwind = "fact-check:publisher:northwind-verify.example"
    review_day = "2099-06-04"
    (known_then,) = store.as_of(h.NS, northwind, scopes=h.SCOPES, day=review_day, known_at_ms=h.OBSERVED["v1"])
    assert known_then["record"]["fields"]["ifcn_status"] == "verified"
    (known_now,) = store.as_of(h.NS, northwind, scopes=h.SCOPES, day=review_day)
    assert known_now["record"]["fields"]["ifcn_status"] == "expired" and known_now["in_force_since"] == "2099-05-10"
    (before,) = store.as_of(h.NS, northwind, scopes=h.SCOPES, day="2099-01-01")
    assert before["record"]["fields"]["ifcn_status"] == "verified"
    assert store.as_of(h.NS, northwind, scopes=h.SCOPES, day="2090-01-01") == []
    key = key_for(store, h.C1, "factdesk.example", h.GOOGLE)
    rows = store.records(h.NS, scopes=h.SCOPES, record_keys=[key], known_at_ms=day_ms("2099-07-31"))
    assert {r["source_id"]: r["revision_no"] for r in rows} == {h.GOOGLE: 1, h.DATACOMMONS: 1}


def test_replays_are_idempotent_and_leave_receipts():
    conn, store = loaded(v2=False)
    again = h.apply(conn, h.DATACOMMONS, run_id="replay")
    assert again[0]["counts"]["unchanged"] == 2 and again[0]["counts"]["new"] == 0
    receipts = store.receipts(h.NS, "replay", scopes=h.SCOPES)
    assert receipts and receipts[0]["receipt"]["requests"][0]["sha256"]


def _record(store):
    view = store.records(h.NS, scopes=h.SCOPES, kinds=["fact-check"], record_keys=[
        key_for(store, h.C1, "factdesk.example", h.DATACOMMONS)])[0]
    return copy.deepcopy(view["record"])


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda r: r["fields"]["claims"][0]["claimant"].update(image="https://images.example/robin.jpg"),
     "minimisation_violation"),
    (lambda r: r["fields"]["claims"][0]["claimant"].update(job_title="Spokesperson"), "minimisation_violation"),
    (lambda r: r["fields"]["claims"][0]["appearances"].append({"url": "https://x.com/someone/status/1",
                                                                "platform_post": True}),
     "minimisation_violation"),
    (lambda r: r["fields"]["claims"][0]["appearances"].append({"url": "https://www.facebook.com/someone/posts/2",
                                                                "platform_post": False}),
     "minimisation_violation"),
    (lambda r: r["fields"].update(review_author="Pat Reviewer"), "minimisation_violation"),
    (lambda r: r["fields"]["claims"][0].update(truth_verdict="false"), "assessment_forbidden"),
    (lambda r: r["fields"]["claims"][0]["rating"].update(normalized_rating="disputed"), "assessment_forbidden"),
    (lambda r: r["fields"].update(claims=[]), "invalid_record"),
])
def test_personal_fields_and_verdicts_are_refused_at_write_time(mutate, code):
    conn, store = loaded(v2=False)
    record = _record(store)
    mutate(record)
    before = conn.execute("SELECT count(*) FROM fact_check_revisions").fetchone()[0]
    with pytest.raises(FactCheckError) as refused:
        store.project(h.NS, [record], run_id="bad", source_id=h.DATACOMMONS)
    assert refused.value.code == code
    assert conn.execute("SELECT count(*) FROM fact_check_revisions").fetchone()[0] == before


def test_reads_require_the_read_scope_and_namespace_access():
    _, store = loaded(v2=False)
    with pytest.raises(FactCheckError):
        store.records(h.NS, scopes={"knowledge:news:fact-checks:read"})
    assert store.records(h.NS, scopes=h.READ_ONLY)
