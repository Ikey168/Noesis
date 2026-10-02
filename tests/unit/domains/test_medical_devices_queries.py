"""A device's regulatory history as of a date and adverse-event report counts with caveats (MD09, MD10)."""

from __future__ import annotations

import pytest

from src.ingestion.medical_devices_sources import MAUDE_CAVEATS
from src.kb.medical_devices_identity import MedicalDeviceIdentity
from src.kb.medical_devices_queries import MedicalDeviceQueries, resolve_subject
from src.kb.medical_devices_records import MedicalDeviceError, forbidden_keys
from tests.unit import medical_devices_harness as h


@pytest.fixture(scope="module")
def reviewed():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn, now=clock)
    h.load_all(conn, v2=True, now=clock)
    identity = MedicalDeviceIdentity(conn)
    for item in identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)["candidates"]:
        identity.review(h.NS, item["candidate_id"], "reject" if item["low_evidence"] else "accept",
                        "a product code is a device type" if item["low_evidence"] else "identifiers agree",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    return conn


def keys(answer, jurisdiction, group):
    return [e["record_key"] for e in answer["jurisdictions"][jurisdiction][group]]


def test_history_as_of_a_date_cites_revisions_and_keeps_jurisdictions_apart(reviewed):
    answer = MedicalDeviceQueries(reviewed).regulatory_history(h.NS, h.PUMP_DI, "2025-08-15", scopes=h.SCOPES)
    assert answer["on_record"] and set(answer["jurisdictions"]) == {"US", "EU"}
    assert keys(answer, "US", "clearances") == [h.PUMP_CLEARANCE]
    (recall,) = answer["jurisdictions"]["US"]["recalls"]
    assert (recall["status_as_published"], recall["recall_class"], recall["terminated"]) == (
        "Open, Classified", "Class II", None)
    assert recall["citation"]["revision_no"] == 1 and recall["citation"]["revision_basis"] == "published by the date"
    (certificate,) = answer["jurisdictions"]["EU"]["certificates"]
    assert certificate["status_as_published"] == "Suspended" and certificate["citation"]["revision_no"] == 2
    assert keys(answer, "EU", "devices") == [h.EU_DEVICE]
    for group in answer["jurisdictions"].values():
        for entries in group.values():
            for entry in entries if isinstance(entries, list) else []:
                cite = entry["citation"]
                assert cite["revision_id"] and cite["locator"] and cite["observed_at_ms"] and entry["reach"]["basis"]
    assert {m["method"] for m in answer["identity_matches_used"]} <= {"udi-di", "premarket-number"}
    later = MedicalDeviceQueries(reviewed).regulatory_history(h.NS, h.PUMP_DI, "2025-10-01", scopes=h.SCOPES)
    (recall,) = later["jurisdictions"]["US"]["recalls"]
    assert (recall["status_as_published"], recall["terminated"]) == ("Terminated", "2025-09-10")


def test_approvals_and_supplements_in_force_at_a_date(reviewed):
    queries = MedicalDeviceQueries(reviewed)
    early = queries.regulatory_history(h.NS, "P999001", "2022-01-01", scopes=h.SCOPES)
    assert keys(early, "US", "approvals") == [h.PMA]
    assert [e["supplement_number"] for e in early["jurisdictions"]["US"]["supplements"]] == ["S001"]
    assert {e["record_key"].rsplit(":", 1)[-1] for e in early["later_events"]} == {"S002", "S003"}
    late = queries.regulatory_history(h.NS, "P999001", "2025-12-31", scopes=h.SCOPES)
    assert [e["supplement_number"] for e in late["jurisdictions"]["US"]["supplements"]] == ["S001", "S002", "S003"]
    assert {e["reach"]["basis"] for e in late["jurisdictions"]["US"]["supplements"]} == {"supplement-of"}


def test_unavailable_modules_missing_providers_and_removals_are_stated(reviewed):
    answer = MedicalDeviceQueries(reviewed).regulatory_history(h.NS, "ZXA", "2025-12-31", scopes=h.SCOPES)
    assert set(answer["gaps"]["eudamed_modules"]) == {"vigilance-post-market-surveillance",
                                                      "clinical-investigations-performance-studies",
                                                      "market-surveillance"}
    assert answer["gaps"]["features_not_selected"] == ["medical-devices-eudamed", "medical-devices-fda",
                                                       "medical-devices-gudid"]
    assert "eudamed" in answer["gaps"]["providers_without_records"]  # a product code reaches no EU record
    assert [r["record_key"] for r in answer["removed_by_source"]] == ["medical-devices:fda:510k:K999002"]
    assert keys(answer, "US", "clearances") == [h.PUMP_CLEARANCE, "medical-devices:fda:510k:K999002"]


def test_a_subject_with_no_records_says_so(reviewed):
    answer = MedicalDeviceQueries(reviewed).regulatory_history(h.NS, "ZZQ", "2025-12-31", scopes=h.SCOPES)
    assert answer["on_record"] is False and "says nothing about the device" in answer["message"]
    assert all(not v for g in answer["jurisdictions"].values() for v in g.values() if isinstance(v, list))
    with pytest.raises(MedicalDeviceError):
        resolve_subject("Exampla FlowSense")  # names are never resolved


def test_report_counts_are_reports_with_caveats_window_and_revisions(reviewed):
    queries = MedicalDeviceQueries(reviewed)
    answer = queries.adverse_event_counts(h.NS, h.PUMP_DI, "2025-01-01", "2025-06-30", scopes=h.SCOPES)
    (published,) = answer["published_counts"]
    assert published["counts_as_published"] == [{"event_type": "Malfunction", "reports": 2},
                                                 {"event_type": "Injury", "reports": 1}]
    assert answer["caveats"] == list(MAUDE_CAVEATS) and "never incidence" in answer["count_semantics"]
    assert answer["query_window"] == {"received_from": "2025-01-01", "received_to": "2025-06-30",
                                      "date_basis": "date FDA received the report"}
    assert published["citation"]["revision_id"] in answer["sources"]
    on_record = answer["reports_on_record"]
    assert on_record["total_reports"] == 3
    assert on_record["by_event_type_and_period"] == [
        {"event_type": "Injury", "period": "2025-03", "reports": 1},
        {"event_type": "Malfunction", "period": "2025-02", "reports": 1},
        {"event_type": "Malfunction", "period": "2025-06", "reports": 1}]
    assert sum(r["names_device_di"] for r in on_record["reports"]) == 2
    assert forbidden_keys(answer) == []
    narrow = queries.adverse_event_counts(h.NS, "ZXA", "2025-03-01", "2025-04-30", scopes=h.SCOPES)
    assert narrow["published_counts"] == [] and len(narrow["published_counts_outside_window"]) == 1
    assert "not split or pro-rated" in narrow["published_counts_outside_window"][0]["note"]
    assert narrow["reports_on_record"]["total_reports"] == 1
    lead = queries.adverse_event_counts(h.NS, h.LEAD_DI, "2025-01-01", "2025-06-30", scopes=h.SCOPES)
    (none,) = lead["published_counts"]  # the ZXB count query was answered with no matches
    assert none["counts_as_published"] == [] and "NOT_FOUND" in none["note"]
    assert lead["published_counts_outside_window"] == [] and lead["reports_on_record"]["total_reports"] == 0
    empty = queries.adverse_event_counts(h.NS, "ZZQ", "2025-01-01", "2025-06-30", scopes=h.SCOPES)
    assert empty["on_record"] is False and "not a statement that no event occurred" in empty["message"]
    assert empty["caveats"]


def test_evidence_bundle_cites_every_item(reviewed):
    bundle = MedicalDeviceQueries(reviewed).evidence_bundle(h.NS, h.PUMP_DI, "2025-12-31", scopes=h.SCOPES,
                                                            received_from="2025-01-01", received_to="2025-06-30")
    assert bundle["items"] and bundle["caveats"]
    for item in bundle["items"]:
        assert item["source"]["locator"].startswith("https://") and item["source"]["provider"]
        assert item["record_revision"]["revision_id"] and item["as_of"]["observed_at_ms"]
    assert "narratives" not in str(bundle["items"])
    assert {i["kind"] for i in bundle["items"]} >= {"clearances", "recalls", "certificates",
                                                    "published-report-counts"}
