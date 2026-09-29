"""Package, graph and organisation monitors through platform.subscriptions with two fixture polls (OS10)."""

from __future__ import annotations

from collections import Counter

import duckdb
import pytest

from src.kb.oss_ecosystem_identity import OssIdentity, repository_key_of
from src.kb.oss_ecosystem_monitoring import OssPackageMonitor
from src.kb.oss_ecosystem_store import OssStoreError
from src.kb.subscriptions import SubscriptionStore
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h

SCOPES = h.READ | {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
NOW = fb.POLL_MS[2] + 86_400_000


def _commit(conn, watermark):
    SubscriptionStore(conn).commit_watermark(h.NS, watermark, kind="ingestion")


def _kinds(result):
    return Counter(n["kind"] for n in result["notifications"])


def test_two_polls_deliver_each_stated_change_once_with_old_and_new_revisions():
    conn = duckdb.connect()
    h.ingest(conn, 1)
    monitor = OssPackageMonitor(conn, now=lambda: NOW)
    package = monitor.create(
        h.NS,
        "parser",
        watch="package",
        key="pkg:pypi:fixture-parser",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    graph = monitor.create(
        h.NS,
        "codec",
        watch="graph",
        key="pkg:cargo:fixture-codec",
        version="0.1.0",
        depth=1,
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    npm = monitor.create(
        h.NS,
        "tokenizer",
        watch="package",
        key="pkg:npm:@fixture-labs/tokenizer",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    _commit(conn, 1)
    first = monitor.run(package, principal_id="alice", scopes=SCOPES)
    assert _kinds(first) == {
        "release_published": 2,
        "dependency_added": 3,
    }  # 1.1.0 adds three
    monitor.run(graph, principal_id="alice", scopes=SCOPES)
    monitor.run(npm, principal_id="alice", scopes=SCOPES)
    h.ingest(conn, 2)
    _commit(conn, 2)
    second = monitor.run(package, principal_id="alice", scopes=SCOPES)
    assert _kinds(second) == {
        "release_published": 1,
        "release_yanked": 1,
        "licence_changed": 1,
        "dependency_removed": 3,
    }
    yank = next(n for n in second["notifications"] if n["kind"] == "release_yanked")
    assert (
        yank["cites"]["source"] == "pypi"
        and yank["cites"]["old_revision_id"] != yank["cites"]["new_revision_id"]
    )
    assert "Broken wheel metadata; use 1.0.0" in yank["message"]
    licence = next(n for n in second["notifications"] if n["kind"] == "licence_changed")
    assert (
        licence["detail"]["from_version"] == "1.1.0"
        and licence["detail"]["version"] == "2.0.0"
    )
    followed = monitor.run(graph, principal_id="alice", scopes=SCOPES)
    assert _kinds(followed) == {
        "release_yanked": 2
    }  # fixture-codec 0.2.0 and fixture-bytes 0.4.1
    tokenizer = monitor.run(npm, principal_id="alice", scopes=SCOPES)
    assert _kinds(tokenizer) == {
        "release_published": 1,
        "release_deprecated": 1,
        "release_unpublished": 1,
        "licence_changed": 1,
        "dependency_removed": 3,
    }
    # Re-acquiring either poll adds nothing and notifies nothing.
    h.ingest(conn, 2)
    h.ingest(conn, 1)
    _commit(conn, 3)
    assert (
        monitor.run(package, principal_id="alice", scopes=SCOPES)["notifications"] == []
    )
    assert (
        len(monitor.poll(package, principal_id="alice", scopes=SCOPES)["events"]) == 11
    )


def test_dependency_and_repository_changes_are_events():
    conn = h.world()
    monitor = OssPackageMonitor(conn, now=lambda: NOW)
    maven = monitor.create(
        h.NS,
        "core",
        watch="package",
        key="pkg:maven:org.fixturelabs:fixture-core",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    parser = monitor.create(
        h.NS,
        "parser",
        watch="package",
        key="pkg:pypi:fixture-parser",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    _commit(conn, 1)
    changes = monitor.run(maven, principal_id="alice", scopes=SCOPES)
    deps = {
        (n["kind"], n["detail"]["dependency"])
        for n in changes["notifications"]
        if "dependency" in n["kind"]
    }
    assert ("dependency_added", "org.fixturelabs:fixture-extra") in deps
    assert ("dependency_removed", "junit:junit") in deps
    monitor.run(parser, principal_id="alice", scopes=SCOPES)
    identity = OssIdentity(conn)
    identity.propose_repositories(h.NS, principal_id="alice", scopes=h.WRITE)
    candidate = identity.candidates(
        h.NS,
        scopes=h.READ,
        key=repository_key_of("github.com/fixture-labs/parser"),
        state="proposed",
    )
    chosen = next(c for c in candidate if c["left_key"].endswith("fixture-parser"))
    identity.review(
        h.NS,
        chosen["candidate_id"],
        "accept",
        "project urls",
        principal_id="rev",
        scopes=h.REVIEW,
    )
    _commit(conn, 2)
    reviewed = monitor.run(parser, principal_id="alice", scopes=SCOPES)
    assert _kinds(reviewed) == {"repository_link_changed": 1}
    identity.revert(
        h.NS, chosen["candidate_id"], "fork", principal_id="rev", scopes=h.REVIEW
    )
    _commit(conn, 3)
    assert _kinds(monitor.run(parser, principal_id="alice", scopes=SCOPES)) == {
        "repository_link_changed": 1
    }


def test_late_older_polls_never_produce_events():
    conn = duckdb.connect()
    h.ingest(conn, 2)
    monitor = OssPackageMonitor(conn, now=lambda: NOW)
    package = monitor.create(
        h.NS,
        "parser",
        watch="package",
        key="pkg:pypi:fixture-parser",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    _commit(conn, 1)
    monitor.run(package, principal_id="alice", scopes=SCOPES)
    h.ingest(conn, 1)  # arrives after poll 2
    _commit(conn, 2)
    assert (
        monitor.run(package, principal_id="alice", scopes=SCOPES)["notifications"] == []
    )


def test_monitors_validate_their_watch_and_need_a_committed_watermark():
    conn = h.world()
    monitor = OssPackageMonitor(conn, now=lambda: NOW)
    with pytest.raises(OssStoreError):
        monitor.create(
            h.NS,
            "x",
            watch="maintainer",
            key="ada",
            principal_id="alice",
            scopes=SCOPES,
        )
    with pytest.raises(OssStoreError):
        monitor.create(
            h.NS,
            "y",
            watch="graph",
            key="pkg:pypi:fixture-parser",
            principal_id="alice",
            scopes=SCOPES,
        )
    organisation = monitor.create(
        h.NS,
        "org",
        watch="organisation",
        key="fixture-labs",
        principal_id="alice",
        scopes=SCOPES,
    )["subscription_id"]
    with pytest.raises(OssStoreError) as caught:
        monitor.run(organisation, principal_id="alice", scopes=SCOPES)
    assert caught.value.code == "watermark_uncommitted"
    _commit(conn, 1)
    packages = {
        n["package"]
        for n in monitor.run(organisation, principal_id="alice", scopes=SCOPES)[
            "notifications"
        ]
    }
    assert packages == {
        "pkg:pypi:fixture-parser",
        "pkg:pypi:fixture-tokens",
        "pkg:cargo:fixture-codec",
        "pkg:cargo:fixture-bytes",
    }
