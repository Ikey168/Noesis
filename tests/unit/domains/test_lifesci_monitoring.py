"""Life-sciences monitors through platform.subscriptions: new, revised and unchanged cases (#2652, LS11 #2706)."""

from __future__ import annotations

import pytest

from src.kb.lifesci_monitoring import LifeSciMonitor, changed_fields
from src.kb.lifesci_records import LifeSciError
from tests.unit import lifesci_harness as h


def test_new_revised_obsoleted_and_removed_records_are_notified_once_with_citations():
    env = h.Env().loaded()
    monitor = LifeSciMonitor(env.conn)
    created = monitor.create(h.NS, "kinase", principal_id="alice", scopes=h.ALL, accessions=["Q9ZZZ1"],
                             targets=["P0DZZ1"], taxa=["999002"])
    assert "no separate scheduler" in created["refresh"]
    baseline = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    kinds = sorted((n["kind"], n["record_key"]) for n in baseline["notifications"])
    assert ("entry_obsoleted", "Q9ZZZ1") in kinds and ("entry_new", "P0DZZ1") in kinds  # successor followed
    assert ("taxon_merged", "999002") in kinds and sum(k == "activity_added" for k, _ in kinds) == 3
    obsoleted = next(n for n in baseline["notifications"] if n["kind"] == "entry_obsoleted")
    assert obsoleted["successors"] == ["uniprot:P0DZZ1"] and obsoleted["new"]["revision_id"]
    assert {r["status"] for r in baseline["receipts"]} == {"complete"}
    # unchanged: a replay at the same watermark and a re-run of the same data deliver nothing
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []
    env.run("lifesci-same")
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []
    # revised: the next releases change the entry and the activities
    env.later()
    heard = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    by_kind = {(n["kind"], n["record_key"]): n for n in heard["notifications"]}
    revised = by_kind[("entry_revised", "P0DZZ1")]
    assert {c["field"] for c in revised["changes"]} >= {"entry_version", "sequence_version", "sequence"}
    assert revised["prior"]["revision_id"] != revised["new"]["revision_id"] and revised["new"]["release"] == "2099_02"
    assert ("activity_added", "99990004") in by_kind and ("activity_removed", "99990002") in by_kind
    # activities republished unchanged in ChEMBL_100 are not news
    assert ("activity_revised", "99990001") not in by_kind
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert polled["events"]


def test_watches_must_reach_acquired_records_and_changes_never_compare_sequences():
    env = h.Env().loaded()
    monitor = LifeSciMonitor(env.conn)
    with pytest.raises(LifeSciError):
        monitor.create(h.NS, "none", principal_id="alice", scopes=h.ALL)
    with pytest.raises(LifeSciError):
        monitor.create(h.NS, "unknown", principal_id="alice", scopes=h.ALL, accessions=["P0DZZ9"])
    assert changed_fields({"sequence": {"value": "MK"}, "release": "1"}, {"sequence": {"value": "MKT"},
                                                                          "release": "2"}) == [
        {"field": "sequence", "change": "the published sequence differs (see sequence_version); no sequence "
                                        "comparison is made"}]
