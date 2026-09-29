"""Medicines monitors through knowledge subscriptions (#2214, MR11): status changes, label revisions, DSC updates."""

from __future__ import annotations

import pytest

from src.kb.clinical_monitoring import MEDICINES_CONTRACT, MedicinesMonitor
from src.kb.clinical_records import ClinicalRecordError
from tests.unit.clinical.harness import NS
from tests.unit.clinical.medicines_harness import READ_ONLY, SCOPES, Env

WATCHER = READ_ONLY | {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_status_change_label_revision_and_dsc_update_are_delivered_once_with_citations():
    env = Env()
    env.serve_earlier()
    env.acquire_medicines("m1")
    env.propose()
    env.accept_eu()
    monitor = MedicinesMonitor(env.conn, now=env.now)
    created = monitor.create_medicine(NS, "noetiglutide", "watch-1", principal_id="alice", scopes=WATCHER)
    assert created["refresh"]["source_pack"] == "clinical-evidence"
    first = monitor.run_medicine(created["subscription_id"], 1, principal_id="alice", scopes=WATCHER)
    assert "label-revision" in kinds(first) and "safety-communication" in kinds(first)
    assert all(n["contract"] == MEDICINES_CONTRACT for n in first["notifications"])
    # Nothing new: deduplicated.
    again = monitor.run_medicine(created["subscription_id"], 2, principal_id="alice", scopes=WATCHER)
    assert again["notifications"] == []
    # A new label revision (with section diffs), a DSC update and a marketing-status change arrive.
    env.serve_pinned()
    env.serve_dsc_update()
    env.serve_discontinued()
    env.acquire_medicines("m2")
    third = monitor.run_medicine(created["subscription_id"], 3, principal_id="alice", scopes=WATCHER)
    got = kinds(third)
    assert "safety-communication-updated" in got and "authorisation-status-change" in got
    labels = [n for n in third["notifications"] if n["kind"] == "label-revision"]
    assert {n["item"]["document"]["version"] for n in labels} == {"4", "8"}
    spl = next(n for n in labels if n["item"]["document"]["kind"] == "spl")
    changes = spl["item"]["section_changes"]
    assert changes["from_revision"]["version"] == "7" and changes["to_revision"]["version"] == "8"
    assert any(c["code"] == "34066-1" and c["change"] == "added" for c in changes["changes"])
    update = next(n for n in third["notifications"] if n["kind"] == "safety-communication-updated")
    assert update["item"]["updates_added"][0]["date"] == "2026-06-02"
    status = next(n for n in third["notifications"] if n["kind"] == "authorisation-status-change")
    assert status["item"]["event"]["status"] == "discontinued" and status["item"]["citation"]["disclaimer"]
    assert "no advice" in status["note"]
    fourth = monitor.run_medicine(created["subscription_id"], 4, principal_id="alice", scopes=WATCHER)
    assert fourth["notifications"] == []
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=WATCHER)
    assert polled


def test_only_the_owner_runs_a_medicines_monitor_and_a_name_is_required():
    env = Env()
    monitor = MedicinesMonitor(env.conn, now=env.now)
    with pytest.raises(ClinicalRecordError):
        monitor.create_medicine(NS, " ", "watch-x", principal_id="alice", scopes=WATCHER)
    created = monitor.create_medicine(NS, "noetiglutide", "watch-2", principal_id="alice", scopes=WATCHER)
    with pytest.raises(ClinicalRecordError):
        monitor.run_medicine(created["subscription_id"], 1, principal_id="mallory", scopes=WATCHER)
    assert SCOPES
