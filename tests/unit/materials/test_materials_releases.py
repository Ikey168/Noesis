"""Release vintages, release diffs and subscription watches with two fixture releases (MT11, #2089)."""

from __future__ import annotations

import pytest

from src.kb.materials_releases import MaterialsReleaseWatch, release_changes
from src.kb.materials_store import MaterialsError, MaterialsStore, record_key
from src.kb.subscriptions import SubscriptionStore
from tests.unit.materials import harness as h

SUBS = {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
SCOPES = h.SCOPES | SUBS
MP1 = record_key("materials-project", "mp-990001")


@pytest.fixture()
def env():
    return h.Env().load()


def test_refresh_appends_a_release_and_the_prior_release_stays_queryable(env):
    first = MaterialsStore(env.conn, initialize=False).snapshot_id(h.NS)
    env.mp_release_2()
    store = MaterialsStore(env.conn, initialize=False)
    assert store.snapshot_id(h.NS) != first
    assert [r["label"] for r in store.releases(h.NS, "materials-project")] == [
        "2099.1.0",
        "2099.2.0",
    ]
    (gap,) = [
        v
        for v in store.current_values(
            h.NS, provider="materials-project", prop="band_gap"
        )
        if v["native_id"] == "mp-990001"
    ]
    assert (
        gap["value"] == "1.800"
        and gap["release"]["label"] == "2099.2.0"
        and gap["change"] == "initial"
    )
    history = store.value_history(h.NS, gap["identity_key"])
    assert [(v["release"]["label"], v["value"]) for v in history] == [
        ("2099.1.0", "1.780"),
        ("2099.2.0", "1.800"),
    ]


def test_release_diff_reports_added_changed_and_source_stated_status(env):
    env.mp_release_2()
    diff = release_changes(env.conn, h.NS, "materials-project", scopes=h.SCOPES)
    assert (diff["from_release"], diff["to_release"]) == ("2099.1.0", "2099.2.0")
    changed = [c for c in diff["changes"] if c["change"] == "changed"]
    assert [
        (c["record_key"], c["property"], c["before"]["value"], c["after"]["value"])
        for c in changed
    ] == [(MP1, "band_gap", "1.780", "1.800")]
    assert (
        changed[0]["before"]["release"] == "2099.1.0"
        and changed[0]["after"]["release"] == "2099.2.0"
    )
    added = {c["record_key"] for c in diff["changes"] if c["change"] == "added"}
    assert added == {record_key("materials-project", "mp-990004")}
    assert diff["record_status_changes"] == [
        {
            "record_key": record_key("materials-project", "mp-990002"),
            "before": "active",
            "after": "deprecated",
            "basis": "status stated by the source",
        }
    ]
    only_gap = release_changes(
        env.conn,
        h.NS,
        "materials-project",
        scopes=h.SCOPES,
        prop="band_gap",
        material=MP1,
    )
    assert [c["change"] for c in only_gap["changes"]] == ["changed"]


def test_watch_notifies_with_both_release_values_and_replay_adds_nothing(env):
    watch = MaterialsReleaseWatch(env.conn)
    created = watch.create(
        h.NS,
        "gap-watch",
        targets={"material": MP1, "property": "band_gap"},
        principal_id="alice",
        scopes=SCOPES,
    )
    subscriptions = SubscriptionStore(env.conn)
    subscriptions.commit_watermark(h.NS, 1, kind="ingestion")
    first = watch.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert {n["kind"] for n in first["notifications"]} == {"value_in_view"}
    env.mp_release_2()
    subscriptions.commit_watermark(h.NS, 2, kind="ingestion")
    second = watch.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    (update,) = [n for n in second["notifications"] if n["kind"] == "new_release_value"]
    assert (
        update["cites"]["before"]["release"] == "2099.1.0"
        and update["cites"]["after"]["release"] == "2099.2.0"
    )
    assert (update["cites"]["before"]["value"], update["cites"]["after"]["value"]) == (
        "1.780",
        "1.800",
    )
    again = watch.run(
        created["subscription_id"], 2, principal_id="alice", scopes=SCOPES
    )
    assert again["status"] == "replayed" and again["notifications"] == []
    polled = watch.poll(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert polled["events"]


def test_watches_need_committed_watermarks_and_known_targets(env):
    watch = MaterialsReleaseWatch(env.conn)
    created = watch.create(
        h.NS,
        "provider-watch",
        targets={"provider": "oqmd"},
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(MaterialsError) as uncommitted:
        watch.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert uncommitted.value.code == "watermark_uncommitted"
    with pytest.raises(MaterialsError):
        watch.create(
            h.NS, "bad", targets={"colour": "red"}, principal_id="alice", scopes=SCOPES
        )
