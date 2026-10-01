"""Medical-device records: immutable revisions, as-of lookup and write-time minimisation (#2665, MD02)."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from src.kb.medical_devices_records import (
    NARRATIVE_SCOPE,
    MedicalDevicesError,
    MedicalDevicesStore,
    readiness,
    validate,
)
from tests.unit import medical_devices_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-medical-device-record-v1.json").read_text())
RECALL = "medical-devices:fda:recall:Z-9901-2099"


def loaded(*, v2: bool = False):
    conn = h.connection()
    h.load_all(conn, observed_at_ms=4_070_908_800_000)  # 2099-01-01
    if v2:
        h.load_all(conn, version="v2", run_id="v2", observed_at_ms=4_096_828_800_000)  # 2099-10-26
    return conn


def test_every_emitted_record_satisfies_the_contract():
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for source_id in h.SOURCES:
        for version in ("v1", "v2"):
            for record in h.fetch_all(source_id, version)[0]:
                assert not list(validator.iter_errors(record)), record["record_key"]


def test_a_changed_publication_is_a_new_revision_and_a_replay_adds_nothing():
    conn = loaded()
    store = MedicalDevicesStore(conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 19
    outcome = h.apply(conn, "devices-fda-recalls")[0]
    assert outcome["counts"]["unchanged"] == 1 and outcome["counts"]["new"] == 0
    h.load_all(conn, version="v2", run_id="v2")
    chain = store.history(h.NS, RECALL, scopes=h.SCOPES, source_id="devices-fda-recalls")
    assert [(r["change"], r["record"]["fields"]["recall_status"]) for r in chain] == [
        ("new", "Open, Classified"), ("revised", "Terminated")]
    assert chain[1]["previous_revision_id"] == chain[0]["revision_id"]
    approval = store.history(h.NS, "medical-devices:fda:pma:P999901", scopes=h.SCOPES)
    assert [r["record"]["fields"]["supplements_as_published"] for r in approval] == [["S001", "S002"],
                                                                                     ["S001", "S002", "S003"]]
    # a source that drops a record never deletes it: the last revision stays on record
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=["medical-devices:fda:pma:P999901:S001"])


def test_an_older_publication_delivered_later_never_becomes_current():
    conn = loaded(v2=True)
    store = MedicalDevicesStore(conn)
    outcome = h.apply(conn, "devices-gudid-identifiers", run_id="late")
    assert outcome[0]["counts"]["unchanged"] == 1  # version 3 is already on record: nothing re-applied
    record = store.records(h.NS, scopes=h.SCOPES, record_keys=[f"medical-devices:gudid:di:{h.EXAMPLE_DI}"])[0]
    assert record["native_revision"] == "version:4"
    older = h.fetch_all("devices-gudid-identifiers")[0][0]
    older = {**older, "fields": {**older["fields"], "brand_name": "EXAMPLEPUMP (re-sent)"}}
    result = store.project(h.NS, [older], run_id="older", source_id="devices-gudid-identifiers")
    assert result["counts"]["older-observation"] == 1
    current = store.records(h.NS, scopes=h.SCOPES, record_keys=[older["record_key"]])[0]
    assert current["native_revision"] == "version:4"


def test_as_of_lookup_by_published_date_and_by_observation():
    conn = loaded(v2=True)
    store = MedicalDevicesStore(conn)
    before = store.as_of(h.NS, RECALL, "2099-05-01", scopes=h.SCOPES, source_id="devices-fda-recalls")
    state = before["sources"][0]
    assert state["status"] == "in_force" and state["revision"]["record"]["fields"]["recall_status"] == "Open, Classified"
    assert [r["date"] for r in state["later_revisions"]] == ["2099-09-15"]
    after = store.as_of(h.NS, RECALL, "2099-09-30", scopes=h.SCOPES, source_id="devices-fda-recalls")["sources"][0]
    assert after["revision"]["record"]["fields"]["recall_status"] == "Terminated"
    early = store.as_of(h.NS, "medical-devices:fda:510k:K999902", "2099-01-01", scopes=h.SCOPES)["sources"][0]
    assert early["status"] == "not_yet_published"  # decided 2099-06-15
    undated = store.as_of(h.NS, "medical-devices:fda:product-code:ZZA", "1990-01-01", scopes=h.SCOPES)["sources"][0]
    assert undated["status"] == "in_force" and undated["dated"] is False
    observed = store.as_of(h.NS, RECALL, "2099-09-30", scopes=h.SCOPES, source_id="devices-fda-recalls",
                           basis="observed")["sources"][0]
    assert observed["revision"]["revision_no"] == 1  # the termination was observed on 2099-10-26
    assert store.as_of(h.NS, "medical-devices:fda:510k:K000000", "2099-01-01", scopes=h.SCOPES)["status"] == \
        "none_on_record"


def test_minimisation_is_enforced_at_write_time():
    record = copy.deepcopy(h.fetch_all("devices-fda-510k")[0][0])
    record["fields"]["contact"] = "SOMEONE"
    with pytest.raises(MedicalDevicesError) as refused:
        validate(record)
    assert refused.value.code == "minimisation_violation" and refused.value.details["paths"] == ["$.fields.contact"]
    report = copy.deepcopy(h.fetch_all("devices-fda-maude-reports")[0][0])
    report["fields"]["patient"] = [{"patient_age": "50"}]
    with pytest.raises(MedicalDevicesError):
        validate(report)
    clearance = copy.deepcopy(h.fetch_all("devices-fda-510k")[0][0])
    clearance["fields"]["narratives"] = [{"text": "x"}]
    with pytest.raises(MedicalDevicesError):
        validate(clearance)
    no_caveats = {**h.fetch_all("devices-fda-maude-counts")[0][0], "caveats": None}
    with pytest.raises(MedicalDevicesError):
        validate(no_caveats)
    no_disclaimer = {**h.fetch_all("devices-fda-510k")[0][0], "disclaimer": None}
    with pytest.raises(MedicalDevicesError):
        validate(no_disclaimer)
    conn = h.connection()
    with pytest.raises(MedicalDevicesError):
        MedicalDevicesStore(conn).project(h.NS, [h.fetch_all("devices-fda-pma")[0][0], record], run_id="r",
                                          source_id="devices-fda-510k")
    assert MedicalDevicesStore(conn).records(h.NS, scopes=h.SCOPES) == []  # the whole page was refused


def test_narratives_are_returned_only_with_the_narrative_scope_and_reads_need_namespace_access():
    conn = loaded()
    store = MedicalDevicesStore(conn)
    plain = store.records(h.NS, scopes=h.SCOPES, kinds=["adverse-event-report"])
    assert all(r["record"]["fields"]["narratives"] is None and r["record"]["fields"]["narratives_withheld"] == 1
               for r in plain)
    full = store.records(h.NS, scopes=h.SCOPES | {NARRATIVE_SCOPE}, kinds=["adverse-event-report"])
    assert all(r["record"]["fields"]["narratives"][0]["as_published"] for r in full)
    with pytest.raises(MedicalDevicesError) as refused:
        store.records(h.NS, scopes={h.READ})
    assert refused.value.code == "unauthorized"
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    state = readiness(conn)
    assert state["providers"]["openfda-device"]["evidence_origins"] == ["fixture"]
    assert all(p["live"] != "verified-live" for p in state["providers"].values())
