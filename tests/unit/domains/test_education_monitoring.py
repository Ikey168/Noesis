"""Monitoring new education releases, revised values and identity-match changes through subscriptions (#2433)."""

from __future__ import annotations

import pytest

from src.kb.education_identity import EducationIdentity
from src.kb.education_monitoring import EducationMonitor
from src.kb.education_statistics import EducationError
from tests.unit import education_harness as h

R1, R3 = "https://ror.org/0zfs01a23", "https://ror.org/0zth03c45"
JUDGEMENTS = ("improv", "declin", "better", "worse", "increase", "decrease", "rank")


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    yield conn, identity, EducationMonitor(conn)
    conn.close()


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_new_vintages_then_revised_values_with_record_ids_and_citations(env):
    conn, _, monitor = env
    created = monitor.create(h.NS, "fsu", watch={"institution": {"scheme": "ipeds-unitid", "code": "100001"}},
                             principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Four statistics series and the institution's two proposed ROR candidates.
    assert kinds(first) == ["identity_match_change"] * 2 + ["new_vintage"] * 4
    assert all(n["source_revision"]["release_stage"] == "provisional" for n in first["notifications"]
               if n["kind"] == "new_vintage")
    h.apply(conn, "ipeds", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    second = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (revised,) = second["notifications"]
    assert revised["kind"] == "revised_value" and revised["source_revision"]["release_stage"] == "final"
    assert revised["changed_values"] == [{"period": "2098", "before": {"value": "25000", "status": "reported",
                                                                       "special_code": None},
                                          "after": {"value": "25140", "status": "reported", "special_code": None}}]
    assert revised["previous_vintage_id"] in revised["record_ids"] and revised["vintage_id"] in revised["record_ids"]
    for notice in first["notifications"] + second["notifications"]:
        assert not any(word in notice["message"].lower() for word in JUDGEMENTS)
    # A restart replays the committed state and emits nothing.
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []


def test_an_unchanged_release_emits_nothing_and_country_watches_see_updates(env):
    conn, _, monitor = env
    created = monitor.create(h.NS, "de", watch={"country": "DE", "providers": ["eurostat-rd"]},
                             principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["new_vintage", "new_vintage"]
    h.apply(conn, "eurostat", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)  # DE unchanged, FR revised
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    france = monitor.create(h.NS, "fr", watch={"indicator": "GERD_HES"}, principal_id="alice", scopes=h.SCOPES)
    notices = monitor.run(france["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert kinds({"notifications": notices}) == ["new_vintage", "new_vintage", "revised_value"]


def test_identity_match_changes_are_events_with_the_match_ids(env):
    _, identity, monitor = env
    created = monitor.create(h.NS, "ror", watch={"ror": R1}, principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert [n["state"] for n in first["notifications"]] == ["candidate"]
    match = next(m for m in identity.matches(h.NS, scopes=h.SCOPES) if m["ror_id"] == R1)
    identity.review(h.NS, match["match_id"], "accept", "same city", principal_id="r", scopes=h.SCOPES)
    second = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    states = {n["kind"]: n for n in second["notifications"]}
    assert states["identity_match_change"]["state"] == "accepted"
    assert match["match_id"] in states["identity_match_change"]["record_ids"]
    assert "new_vintage" in states  # the accepted match now brings the institution's series into the watch
    status = monitor.create(h.NS, "muster", watch={"ror": R3}, principal_id="alice", scopes=h.SCOPES)
    monitor.run(status["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    identity.record_ror(h.NS, h.ror_records(later=True), principal_id="svc", scopes=h.SCOPES)
    changed = monitor.run(status["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert [n["status"] for n in changed] == [{"before": "active", "after": "inactive"}]


def test_refresh_stays_within_budgets_and_watches_are_validated(env):
    conn, _, monitor = env
    from src.ingestion.education_sources import fixture_transport

    receipt = monitor.refresh(h.NS, h.source("uis"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("uis")))
    assert receipt["status"] == "complete" and receipt["unchanged_releases"] == 1
    limited = [dict(p, status=429, headers={"Retry-After": "600"}) for p in h.pages("eter")]
    stopped = monitor.refresh(h.NS, h.source("eter"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(limited))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = monitor.refresh(h.NS, h.source("eter"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("eter")))
    assert waiting["status"] == "rate_limited_wait" and waiting["releases"] == []
    with pytest.raises(EducationError):
        monitor.create(h.NS, "bad", watch={"country": "DE", "indicator": "X"}, principal_id="a", scopes=h.SCOPES)
