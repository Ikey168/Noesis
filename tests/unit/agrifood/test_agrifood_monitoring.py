"""AF10: releases, revisions and linked food alerts delivered through subscriptions (#2362)."""

from __future__ import annotations

import pytest

from src.kb.agrifood_identity import AgrifoodIdentity
from src.kb.agrifood_links import AgrifoodLinks
from src.kb.agrifood_monitoring import AgrifoodMonitor
from src.kb.agrifood_records import AgrifoodError
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    identity = AgrifoodIdentity(item.conn, now=item.tick)
    identity.register_places(principal_id="curator", scopes=h.ALL)
    for candidate in identity.propose(NS, principal_id="matcher", scopes=h.WRITE)["crosswalks"]:
        if {candidate["left"]["scheme"], candidate["right"]["scheme"]} <= {"faostat-item", "nass-commodity",
                                                                           "psd-commodity"}:
            identity.review(NS, candidate["crosswalk_id"], "accept", "same crop", principal_id="rev", scopes=h.REVIEW)
    yield item
    item.conn.close()


def _kinds(result):
    out = {}
    for note in result["notifications"]:
        out.setdefault(note["kind"], []).append(note)
    return out


def test_release_revision_linked_alert_and_deduplication(env):
    monitor = AgrifoodMonitor(env.conn, now=env.tick)
    created = monitor.create(NS, "maize-us", commodity="maize", place="United States", principal_id="alice",
                             scopes=h.ALL)
    assert ["faostat-item", "56"] in created["watch"]["commodity_codes"]
    assert created["watch"]["thresholds"] == {}
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert first["baseline"] and set(_kinds(first)) == {"release"}
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []

    assert env.later()["status"] == "complete"
    second = _kinds(monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL))
    revisions = {(n["new"]["citation"]["release_key"], n["period"]): n for n in second["revision"]}
    fao = revisions[("faostat:QCL:2025-03-20", "2023")]
    assert fao["prior"]["value"] == "389694460" and fao["new"]["value"] == "389667000"
    assert fao["prior"]["citation"]["release_key"] == "faostat:QCL:2024-12-18" and fao["new"]["flag"]["code"] == "A"
    assert "was 389694460" in fao["message"]
    assert ("psd:2025-10", "2025") in revisions
    assert ("psd:2025-10", "2024") not in revisions  # re-released unchanged: no event
    assert any(k[0].startswith("nass:load_time:2024-09-30") for k in revisions)
    replay = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert replay["notifications"] == []  # duplicates are never delivered

    env.seed_rasff()
    AgrifoodLinks(env.conn, now=env.tick).link_rasff(NS, scopes=h.ALL, principal_id="linker")
    alerts = _kinds(monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL))
    assert [n["new"]["notice_number"] for n in alerts["linked_alert"]] == ["2026.1101"]
    assert "2026.1101" in alerts["linked_alert"][0]["message"]
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert polled


def test_thresholds_are_the_users_own(env):
    monitor = AgrifoodMonitor(env.conn, now=env.tick)
    with pytest.raises(AgrifoodError):
        monitor.create(NS, "bad", commodity="maize", place="United States", principal_id="alice", scopes=h.ALL,
                       thresholds={"auto": 1})
    created = monitor.create(NS, "maize-us-pct", commodity="maize", place="United States", principal_id="alice",
                             scopes=h.ALL, thresholds={"min_pct_change": "0.1"})
    monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert env.later()["status"] == "complete"
    revisions = _kinds(monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)).get("revision", [])
    assert [(n["provider"], n["period"]) for n in revisions] == [("fas-psd", "2025")]  # 0.41 %; others below 0.1 %
    with pytest.raises(AgrifoodError):
        monitor.create(NS, "nowhere", commodity="maize", place="Atlantis", principal_id="alice", scopes=h.ALL)
