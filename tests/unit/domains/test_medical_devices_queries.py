"""A device's regulatory history as of a date and adverse-event report counts with caveats (#2699, #2704;
MD09, MD10)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.medical_devices_sources import MAUDE_CAVEATS
from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.medical_devices_queries import MedicalDevicesQueries
from src.kb.medical_devices_records import MedicalDevicesError, forbidden_keys
from tests.unit import medical_devices_harness as h

WORDS = ("safety signal", "incidence", "causal", "risk score", "we recommend", "you should")


@pytest.fixture()
def ask():
    conn = h.connection()
    h.load_all(conn)
    identity = MedicalDevicesIdentity(conn)
    for candidate in identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)["candidates"]:
        if candidate["method"] == "udi-di":
            identity.review(h.NS, candidate["candidate_id"], "accept", "same DI", principal_id="rev",
                            scopes=h.REVIEW_SCOPES)
    return conn, MedicalDevicesQueries(conn)


def keys(events):
    return [e["record_key"] for e in events]


def test_history_shows_jurisdictions_separately_with_cited_revisions(ask):
    _, q = ask
    answer = q.regulatory_history(h.NS, h.FIXTURE_DI, scopes=h.SCOPES, as_of="2099-12-31")
    us, eu = answer["jurisdictions"]["US"], answer["jurisdictions"]["EU"]
    assert keys(us["clearances"]) == ["medical-devices:fda:510k:K999902"]
    assert keys(us["classification"]) == ["medical-devices:fda:product-code:ZZA"] and us["recalls"] == []
    assert keys(eu["devices"]) == ["medical-devices:eudamed:basic-udi-di:4099999FIXTUREFLOWA1"]
    assert keys(eu["certificates"]) == ["medical-devices:eudamed:certificate:9999-MDR-0001"]
    assert eu["devices"][0]["reached_by"].startswith("accepted identity match")
    for bucket in (*us.values(), *eu.values()):
        for event in bucket:
            assert event["citation"]["revision_id"] and event["citation"]["observed_at"]
            assert event["citation"]["source_id"] and event["citation"]["locator"].startswith("https://")
    assert {u["module"] for u in answer["unavailable"] if "module" in u} == {
        "EUDAMED market-surveillance", "EUDAMED vigilance-post-market-surveillance",
        "EUDAMED clinical-investigations-performance-studies"}
    assert answer["identity"]["state"] == "matched" and forbidden_keys(answer) == []


def test_approvals_list_the_supplements_in_force_and_later_decisions(ask):
    _, q = ask
    early = q.regulatory_history(h.NS, "ZZB", scopes=h.SCOPES, as_of="2098-12-31")
    (approval,) = early["jurisdictions"]["US"]["approvals"]
    assert [s["supplement_number"] for s in approval["supplements"]] == ["S001"]
    assert "medical-devices:fda:pma:P999901:S002" in keys(early["later_events"])
    late = q.regulatory_history(h.NS, "P999901", scopes=h.SCOPES, as_of="2099-12-31")
    (approval,) = late["jurisdictions"]["US"]["approvals"]
    assert [s["supplement_number"] for s in approval["supplements"]] == ["S001", "S002"]
    before = q.regulatory_history(h.NS, "ZZB", scopes=h.SCOPES, as_of="2090-01-01")
    assert before["jurisdictions"]["US"]["approvals"] == []  # not yet approved, classification only
    assert keys(before["jurisdictions"]["US"]["classification"]) == ["medical-devices:fda:product-code:ZZB"]


def test_recall_status_and_class_are_as_published_per_source_and_date(ask):
    conn, q = ask
    answer = q.regulatory_history(h.NS, "K999901", scopes=h.SCOPES, as_of="2099-05-01")
    recalls = {e["source_id"]: e for e in answer["jurisdictions"]["US"]["recalls"]}
    assert recalls["devices-fda-recalls"]["status_as_published"] == "Open, Classified"
    assert recalls["devices-fda-enforcement"]["recall_class_as_published"] == "Class II"
    h.load_all(conn, version="v2", run_id="v2")
    before = q.regulatory_history(h.NS, "Z-9901-2099", scopes=h.SCOPES, as_of="2099-05-01")
    after = q.regulatory_history(h.NS, "Z-9901-2099", scopes=h.SCOPES, as_of="2099-10-01")
    assert {e["status_as_published"] for e in before["jurisdictions"]["US"]["recalls"]} == {"Open, Classified",
                                                                                           "Ongoing"}
    assert {e["status_as_published"] for e in after["jurisdictions"]["US"]["recalls"]} == {"Terminated"}
    assert {e["date"] for e in before["later_events"]} == {"2099-09-15"}


def test_a_subject_with_no_records_is_none_on_record(ask):
    _, q = ask
    for subject in ("ZZC", "K000001", "00000000000000"):
        answer = q.regulatory_history(h.NS, subject, scopes=h.SCOPES, as_of="2099-12-31")
        assert answer["status"] == "none_on_record" and answer["note"]
    counts = q.adverse_event_counts(h.NS, "ZZC", scopes=h.SCOPES)
    assert counts["status"] == "none_on_record" and counts["caveats"] == list(MAUDE_CAVEATS)


def test_report_counts_are_reports_with_caveats_window_and_source_revisions(ask):
    _, q = ask
    answer = q.adverse_event_counts(h.NS, h.EXAMPLE_DI, scopes=h.SCOPES, window_from="2099-01-01",
                                    window_to="2099-06-30")
    assert answer["unit"] == "reports" and answer["caveats"] == list(MAUDE_CAVEATS)
    assert answer["query_window"] == {"from": "2099-01-01", "to": "2099-06-30"}
    q1, q2 = answer["published_counts"]
    assert q1["period"] == {"from": "2099-01-01", "to": "2099-03-31"} and q1["reports_in_period_as_published"] == 17
    assert q2["reports_by_event_type_as_published"][-1] == {"event_type_as_published": "No answer provided",
                                                            "reports": 1}
    on_record = answer["reports_on_record"]
    assert on_record["total_reports"] == 3
    assert [(b["period"], b["event_type_as_published"], b["reports"]) for b in on_record["by_period_and_event_type"]] \
        == [("2099-01", "Malfunction", 1), ("2099-03", "Malfunction", 1), ("2099-05", "Injury", 1)]
    assert len(answer["source_revisions"]) == 5
    assert forbidden_keys(answer) == []
    text = json.dumps({k: v for k, v in answer.items() if k not in {"caveats", "exclusions", "notice"}}).lower()
    assert not any(word in text for word in WORDS)
    narrow = q.adverse_event_counts(h.NS, "ZZA", scopes=h.SCOPES, window_from="2099-04-01", window_to="2099-06-30")
    assert [c["period"]["from"] for c in narrow["published_counts"]] == ["2099-04-01"]
    assert narrow["reports_on_record"]["total_reports"] == 1
    assert "narratives" not in json.dumps(answer)


def test_evidence_bundles_cite_every_item_and_reads_need_the_read_scope(ask):
    _, q = ask
    history = q.regulatory_history(h.NS, h.FIXTURE_DI, scopes=h.SCOPES, as_of="2099-12-31")
    bundle = q.evidence_bundle(history)
    assertions = bundle["sections"][0]["assertions"]
    assert len(assertions) == sum(len(e) for b in history["jurisdictions"].values() for e in b.values())
    assert all(a["citations"] and a["dependencies"][0]["revision"] for a in assertions)
    assert {b["id"] for b in bundle["bibliography"]} == {c for a in assertions for c in a["citations"]}
    counts = q.evidence_bundle(q.adverse_event_counts(h.NS, "ZZA", scopes=h.SCOPES))
    assert counts["caveats"] == list(MAUDE_CAVEATS) and "reports, not rates" in counts["sections"][0][
        "assertions"][0]["text"]
    with pytest.raises(MedicalDevicesError):
        q.regulatory_history(h.NS, "ZZA", scopes={f"namespace:{h.NS}:read"})
