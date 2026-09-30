"""Offline acceptance: a medicine to a cited regulatory timeline with label diffs and linked trials (#2214, MR13).

The pinned ``clinical-evidence`` fixtures (trials and FAERS counts, the EMA export
with product information and a withdrawal statement, Drugs@FDA, DailyMed SPL
history and documents, an FDA Drug Safety Communication) and authored RxNav
answers replay through the real source-pack runtime, adapters, clinical record
store, identity review, citation linker, as-of queries and monitors. No request
leaves the process; receipts say ``injected``. Live coverage is MR14 (#2429).
"""

from __future__ import annotations

import pytest

from src.kb.clinical_medicines import BOUNDARY
from src.kb.clinical_monitoring import MedicinesMonitor
from src.kb.clinical_records import ClinicalRecordStore
from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical.harness import NS
from tests.unit.clinical.medicines_harness import READ_ONLY, SCOPES, Env


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_medicine_to_a_cited_regulatory_timeline_with_label_diffs_and_linked_trials():
    env = Env()
    journey = env.journey()
    assert journey["first"]["status"] == journey["second"]["status"] == "complete"
    store = ClinicalRecordStore(env.conn, initialize=False)
    for provider in ("ema-epar", "drugs-at-fda", "dailymed", "fda-dsc", "rxnorm"):
        assert store.provider_state(NS, provider)["last_execution"] == "injected", provider
    service = env.service()

    # Identity: US names exact in RxNorm, the EU product through its reviewed active substance, fixturamab unmatched.
    resolved = service.reach(NS, "noetiglutide", scopes=SCOPES)[0]
    assert {c["rxcui"] for c in resolved["concepts"]} == {"9990001", "9990002"}
    kinds = {s["subject_key"]: (s["match"]["kind"], s["match"]["basis"]) for s in resolved["subjects"]}
    assert kinds["ema:EMEA/H/C/009001"] == ("narrower", "reviewed-active-substance")
    assert kinds["openfda:NDA299001"][0] == "equivalent"
    assert [u["subject_key"] for u in journey["proposed"]["unmatched"]] == ["ema:EMEA/H/C/009002"]

    # Status as of a date per jurisdiction, cited.
    status = service.status_as_of(NS, "noetiglutide", "2026-09-01", scopes=READ_ONLY)
    by = {a["jurisdiction"]: a for a in status["jurisdictions"]}
    assert by["EU"]["status"] == "authorised" and by["EU"]["event"]["effective_date"] == "2020-02-10"
    assert by["US"]["status"] == "approved" and by["US"]["citation"]["disclaimer"]
    withdrawn = service.status_as_of(NS, "EMEA/H/C/009002", "2025-01-01", scopes=READ_ONLY)["jurisdictions"][0]
    assert withdrawn["status"] == "withdrawn" and "commercial reasons" in withdrawn["event"]["reason"]["text"]
    assert service.status_as_of(NS, "EMEA/H/C/009002", "2024-01-01", scopes=READ_ONLY)["jurisdictions"][0][
        "status"] == "authorised"

    # Label text as of a date: the revision in force, dosing never quoted.
    early = {a["provider"]: a for a in service.label_as_of(NS, "noetiglutide", "2025-06-01",
                                                          scopes=READ_ONLY)["labels"]}
    assert early["ema"]["document"]["version"] == "3" and early["dailymed"]["document"]["version"] == "8"
    assert "never retained" not in str(early)

    # Section diffs quoting both revisions.
    changed = service.what_changed(NS, "noetiglutide", scopes=SCOPES)
    smpc = next(d for d in changed["diffs"] if d["document"]["kind"] == "smpc")
    warning = next(c for c in smpc["changes"] if c["code"] == "4.4")
    assert warning["before"]["text"].startswith("Noetiglutide has not been studied")
    assert warning["after"]["text"].startswith("Acute pancreatitis")
    assert smpc["from_revision"]["version"] == "3" and smpc["to_revision"]["version"] == "4"

    # Communications naming the substance, with the match used.
    (communication,) = service.communications(NS, "noetiglutide", scopes=READ_ONLY)["communications"]
    assert communication["issued"] == "2026-04-15" and communication["identity_match"]["kind"] == "equivalent"

    # The cited timeline: chronological, sources side by side, diffs attached, trials by explicit citation.
    timeline = service.timeline(NS, "noetiglutide", scopes=SCOPES, view_id="acceptance-timeline")
    dates = [e["date"] for e in timeline["events"]]
    assert dates == sorted(dates) and dates[0] == "2019-09-05"
    assert {e["authority"] for e in timeline["events"]} == {"EMA", "FDA"}
    assert all(e["citation"]["source_url"].startswith("https://") for e in timeline["events"])
    assert any(e.get("section_changes") for e in timeline["events"] if e["kind"] == "label-revision")
    trials = {(t["from"]["provider"], t["to"]["identifier"]) for t in timeline["linked_trials"]}
    assert ("ema", "NCT09000001") in trials and ("dailymed", "NCT09000001") in trials
    assert all(t["evidence_kind"] == "regulator-cited-reference" for t in timeline["linked_trials"])
    assert timeline["faers_reporting_counts"] and timeline["boundary"] == BOUNDARY

    # A medicine with no record is reported as having none on record.
    none = service.timeline(NS, "unrecordedumab", scopes=READ_ONLY)
    assert none["on_record"] is False and none["message"].startswith("No regulatory record")

    # A later DSC update reaches a monitor once; re-acquisition adds no revision.
    monitor = MedicinesMonitor(env.conn, now=env.now)
    watch = monitor.create_medicine(NS, "noetiglutide", "acceptance", principal_id="alice", scopes=SCOPES)
    monitor.run_medicine(watch["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    env.serve_dsc_update()
    env.acquire_medicines("m3")
    update = monitor.run_medicine(watch["subscription_id"], 2, principal_id="alice", scopes=SCOPES)
    assert [n["kind"] for n in update["notifications"]] == ["safety-communication-updated"]
    before = env.conn.execute("SELECT count(*) FROM clinical_record_revisions").fetchone()[0]
    env.acquire_medicines("m4")
    assert env.conn.execute("SELECT count(*) FROM clinical_record_revisions").fetchone()[0] == before
    assert monitor.run_medicine(watch["subscription_id"], 3, principal_id="alice", scopes=SCOPES)[
        "notifications"] == []
    assert fb.SET_ID in {e["document"]["id"] for e in timeline["events"] if e["kind"] == "label-revision"}
