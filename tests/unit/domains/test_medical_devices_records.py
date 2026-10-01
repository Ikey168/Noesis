"""Medical device records: revision chains, as-of lookup, removals as revisions and write-time minimisation (MD02)."""

from __future__ import annotations

import copy

import pytest

from src.kb.medical_devices_records import (
    NARRATIVE_SCOPE,
    MedicalDeviceError,
    MedicalDeviceStore,
    forbidden_keys,
    personal_fields,
    validate,
)
from tests.unit import medical_devices_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn, now=clock)
    return conn, clock


def _record(conn, key):
    return MedicalDeviceStore(conn, initialize=False).records(h.NS, scopes=h.SCOPES, record_keys=[key])[0]


def test_every_record_carries_source_revision_and_as_of_time(loaded):
    conn, _ = loaded
    rows = MedicalDeviceStore(conn).records(h.NS, scopes=h.SCOPES)
    assert {r["record_kind"] for r in rows} == {
        "classification", "clearance", "approval", "approval-supplement", "recall", "adverse-event-report",
        "report-count", "device-identifier", "eudamed-actor", "eudamed-device", "eudamed-certificate"}
    for row in rows:
        cite = row["citation"]
        assert cite["provider"] and cite["locator"].startswith("https://") and cite["revision_id"]
        assert cite["revision_no"] >= 1 and cite["observed_at_ms"] > 0 and cite["evidence_origin"] == "fixture"
        if row["publication_state"] == "published":
            assert cite["as_of"], row["record_key"]
            if row["provider"] == "openfda-device":
                assert cite["disclaimer"].startswith("Do not rely on openFDA")


def test_kind_specific_fields_are_kept_as_published(loaded):
    conn, _ = loaded
    clearance = _record(conn, h.PUMP_CLEARANCE)["record"]["fields"]
    assert (clearance["k_number"], clearance["decision_code"], clearance["decision_date"], clearance["product_code"]) \
        == ("K999001", "SESE", "2021-03-15", "ZXA")
    supplement = _record(conn, h.PMA + ":S001")["record"]["fields"]
    assert supplement["approval_key"] == h.PMA and supplement["supplement_type"] == "30-Day Notice"
    recall = _record(conn, h.RECALL)["record"]["fields"]
    assert (recall["recall_class"], recall["status_as_published"]) == ("Class II", "Open, Classified")
    report = _record(conn, "medical-devices:fda:mdr:9999001-2025-00002")["record"]
    assert report["fields"]["event_type"] == "Injury" and report["caveats"]
    device = _record(conn, h.GUDID_PUMP)["record"]["fields"]
    assert device["primary_di"] == h.PUMP_DI and device["public_version_number"] == "1"
    assert device["premarket_submissions"] == [{"submission_number": "K999001", "supplement_number": None}]


def test_revision_chains_are_immutable_and_replays_add_nothing(loaded):
    conn, clock = loaded
    store = MedicalDeviceStore(conn)
    before = conn.execute("SELECT count(*) FROM medical_device_revisions").fetchone()[0]
    h.load_all(conn, now=clock)  # replay
    assert conn.execute("SELECT count(*) FROM medical_device_revisions").fetchone()[0] == before
    h.load_all(conn, v2=True, now=clock)
    history = store.history(h.NS, h.RECALL, scopes=h.SCOPES)
    assert [v["change"] for v in history] == ["new", "revised"]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    assert [v["record"]["fields"]["status_as_published"] for v in history] == ["Open, Classified", "Terminated"]
    certificate = store.history(h.NS, h.CERTIFICATE, scopes=h.SCOPES)
    assert [v["record"]["fields"]["status_as_published"] for v in certificate] == ["Valid", "Suspended"]
    # A dataset-wide revision stamp alone (openFDA meta.last_updated) is not a new revision.
    assert len(store.history(h.NS, h.PMA, scopes=h.SCOPES)) == 1
    assert [v["change"] for v in store.history(h.NS, h.PMA + ":S003", scopes=h.SCOPES)] == ["new"]


def test_a_removal_by_the_source_is_a_revision_never_a_deletion(loaded):
    conn, clock = loaded
    h.load_all(conn, v2=True, now=clock)
    history = MedicalDeviceStore(conn).history(h.NS, "medical-devices:fda:510k:K999002", scopes=h.SCOPES)
    assert [(v["change"], v["publication_state"]) for v in history] == [("new", "published"),
                                                                       ("revised", "not-published")]
    assert history[0]["record"]["fields"]["decision_date"] == "2023-06-20"  # still addressable


def test_an_older_version_observed_later_never_becomes_current(loaded):
    conn, clock = loaded
    h.load_all(conn, v2=True, now=clock)
    h.apply(conn, "clinical-devices-accessgudid", run_id="run:late-v1", now=clock)
    store = MedicalDeviceStore(conn)
    history = store.history(h.NS, h.GUDID_PUMP, scopes=h.SCOPES)
    assert [v["change"] for v in history] == ["new", "revised"]  # the v1 replay is already on record
    older = copy.deepcopy(history[0]["record"])
    older["fields"]["device_description"] = "an older text observed late"
    store.project(h.NS, [older], run_id="run:older", source_id="clinical-devices-accessgudid")
    history = store.history(h.NS, h.GUDID_PUMP, scopes=h.SCOPES)
    assert history[-1]["change"] == "older-observation"
    assert _record(conn, h.GUDID_PUMP)["record"]["fields"]["public_version_number"] == "2"


def test_as_of_lookup_by_source_date_and_by_record_time(loaded):
    conn, clock = loaded
    first_loaded = clock.value
    h.load_all(conn, v2=True, now=clock)
    store = MedicalDeviceStore(conn)
    assert store.as_of(h.NS, h.RECALL, scopes=h.SCOPES, published_by="2025-08-01")["record"]["fields"][
        "status_as_published"] == "Open, Classified"
    assert store.as_of(h.NS, h.RECALL, scopes=h.SCOPES, published_by="2025-10-01")["record"]["fields"][
        "status_as_published"] == "Terminated"
    assert store.as_of(h.NS, h.RECALL, scopes=h.SCOPES, published_by="2025-01-01") is None
    assert store.as_of(h.NS, h.RECALL, scopes=h.SCOPES, known_at_ms=first_loaded)["revision_no"] == 1
    assert store.as_of(h.NS, h.RECALL, scopes=h.SCOPES)["revision_no"] == 2


def test_minimisation_is_enforced_at_write_time(loaded):
    conn, _ = loaded
    store = MedicalDeviceStore(conn)
    report = copy.deepcopy(_record(conn, "medical-devices:fda:mdr:9999001-2025-00001")["record"])
    for field, value in (("patient", [{"patient_age": "63"}]), ("manufacturer_contact_f_name", "Jane"),
                         ("address_1", "1 Example Way")):
        bad = copy.deepcopy(report)
        bad["fields"][field] = value
        with pytest.raises(MedicalDeviceError) as caught:
            store.project(h.NS, [bad], run_id="run:bad", source_id="x")
        assert caught.value.code == "minimisation_violation"
    bad = copy.deepcopy(report)
    bad["fields"]["devices"][0]["email"] = "x@example.invalid"
    with pytest.raises(MedicalDeviceError):
        validate(bad)
    # Nothing that the fixtures carry (patients, contacts, PRRCs, addresses) survives acquisition.
    for row in store.records(h.NS, scopes={"operator"}):
        assert personal_fields(row["record"]) == [], row["record_key"]
    assert conn.execute("SELECT count(*) FROM medical_device_revisions WHERE run_id='run:bad'").fetchone()[0] == 0


def test_narratives_need_the_narrative_scope(loaded):
    conn, _ = loaded
    store = MedicalDeviceStore(conn)
    key = "medical-devices:fda:mdr:9999001-2025-00001"
    hidden = store.records(h.NS, scopes=h.SCOPES, record_keys=[key])[0]
    assert all(n["text"] is None and n["withheld"] for n in hidden["record"]["fields"]["narratives"])
    assert hidden["narratives_withheld"]["scope"] == NARRATIVE_SCOPE
    shown = store.records(h.NS, scopes=h.NARRATIVE_SCOPES, record_keys=[key])[0]
    assert "OCCLUSION ALARM" in shown["record"]["fields"]["narratives"][0]["text"]
    assert store.history(h.NS, key, scopes=h.SCOPES)[0]["record"]["fields"]["narratives"][0]["text"] is None


def test_records_refuse_assessments_and_need_disclaimer_and_caveats(loaded):
    conn, _ = loaded
    report = copy.deepcopy(_record(conn, "medical-devices:fda:mdr:9999001-2025-00002")["record"])
    with pytest.raises(MedicalDeviceError) as caught:
        validate({**report, "fields": {**report["fields"], "causality": "device caused"}})
    assert caught.value.code == "assessment_forbidden"
    with pytest.raises(MedicalDeviceError) as caught:
        validate({**report, "caveats": []})
    assert caught.value.code == "missing_caveats"
    clearance = copy.deepcopy(_record(conn, h.PUMP_CLEARANCE)["record"])
    with pytest.raises(MedicalDeviceError) as caught:
        validate({**clearance, "disclaimer": None})
    assert caught.value.code == "missing_disclaimer"
    assert forbidden_keys({"a": {"incidence": 1}}) == ["$.a.incidence"]


def test_reads_need_clinical_read_and_namespace_access(loaded):
    conn, _ = loaded
    store = MedicalDeviceStore(conn)
    with pytest.raises(MedicalDeviceError):
        store.records(h.NS, scopes={"knowledge:clinical:read"})
    with pytest.raises(MedicalDeviceError):
        store.history("other", h.RECALL, scopes=h.READ_ONLY)
    assert store.records(h.NS, scopes=h.READ_ONLY)
