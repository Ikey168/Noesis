"""AI10 (#2795, track #2742): subscriptions notice new revisions, licence changes, gating and removals, offline."""

from __future__ import annotations

import pytest

from src.ingestion.ai_models_sources import fixture_transport
from src.kb.ai_models_monitoring import AiModelsMonitor, change_kinds
from src.kb.ai_models_records import AiModelsError
from src.kb.ai_models_store import AiModelsStore
from tests.unit import ai_models_harness as h

A, B, C, D, E = (h.SHAS[k] for k in "ABCDE")


def refresh(monitor, name, *, revision=False, pages=None, at=h.FIRST_RETRIEVAL):
    return monitor.refresh(h.NS, h.source(name), principal_id="svc", scopes=h.SCOPES,
                           transport=fixture_transport(pages or h.pages(name, revision)), retrieved_at_ms=at)


def kinds(result):
    return sorted((n["kind"], n["record"]["native_key"]) for n in result["notifications"])


def test_monitors_are_subscriptions_and_notices_cite_the_revisions_and_state_what_changed():
    conn = h.connection()
    monitor = AiModelsMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    for name in h.SOURCES:
        assert refresh(monitor, name)["status"] == "complete"
    model = monitor.create(h.NS, "watch-model", target={"repo_id": h.MODEL, "kind": "model"}, principal_id="alice",
                           scopes=h.SCOPES)
    hub = monitor.create(h.NS, "watch-hub", target={"source": "huggingface-hub"}, principal_id="alice",
                         scopes=h.SCOPES)
    epoch = monitor.create(h.NS, "watch-epoch", target={"epoch_model": "Fixture Small Model"}, principal_id="alice",
                           scopes=h.SCOPES)
    assert model["subscription_id"].startswith("subscription:") and "no new scheduler" in model["refresh"]
    first = monitor.run(model["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == [("licence_change", h.MODEL), ("new_revision", h.MODEL), ("new_revision", h.MODEL)]
    licence = next(n for n in first["notifications"] if n["kind"] == "licence_change")
    assert licence["what_changed"]["declared_before"] == "apache-2.0"
    assert licence["what_changed"]["declared_after"] == "other"
    assert licence["citation"]["revision"]["sha"] == B and licence["previous_revision_id"]
    monitor.run(hub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    monitor.run(epoch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Unchanged: the same sources again add no revision and no notice.
    for name in h.SOURCES:
        again = refresh(monitor, name, at=h.SECOND_RETRIEVAL)
        assert again["new_units"] == 0 and again["status"] == "complete"
    assert monitor.run(model["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # New, revised, gated and removed.
    for name in h.SOURCES:
        refresh(monitor, name, revision=True, at=h.SECOND_RETRIEVAL)
    revised = monitor.run(model["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(revised) == [("licence_change", h.MODEL), ("new_revision", h.MODEL)]
    new = next(n for n in revised["notifications"] if n["kind"] == "new_revision")
    assert new["citation"]["revision"]["sha"] == E and new["what_changed"]["first_revision"] is False
    assert next(n for n in revised["notifications"] if n["kind"] == "licence_change")["what_changed"][
        "after"]["license_name"] == "fixture-model-licence-2.0"
    gated = monitor.run(hub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert ("withdrawn", h.SMALL) in kinds(gated)
    withdrawn = next(n for n in gated["notifications"] if n["kind"] == "withdrawn")
    assert withdrawn["what_changed"]["gated"] == "manual" and withdrawn["citation"]["time_basis"] == "retrieval_time"
    removed = monitor.run(epoch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(removed) == [("removed_by_source", "Fixture Small Model")]
    assert "missing from a complete later file" in removed["notifications"][0]["what_changed"]["basis"]
    assert all("assess" not in n["message"].lower() for n in gated["notifications"])
    # A replay of the same watermark adds nothing.
    assert monitor.run(hub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []


def test_a_failed_refresh_records_a_receipt_and_never_produces_a_removal_notice():
    conn = h.connection()
    monitor = AiModelsMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    refresh(monitor, "hub")
    refresh(monitor, "epoch")
    watch = monitor.create(h.NS, "watch-all", target={"source": "epoch-ai"}, principal_id="alice", scopes=h.SCOPES)
    monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    before = AiModelsStore(conn).generation(h.NS)
    failing = [{**p, "status": 503, "body": None} if p["request"].endswith(".csv") else p
               for p in h.pages("epoch", revision=True)]
    receipt = refresh(monitor, "epoch", pages=failing, at=h.SECOND_RETRIEVAL)
    assert receipt["status"] == "stopped" and receipt["stopped"]["code"] == "source_unavailable"
    assert AiModelsStore(conn).generation(h.NS) == before
    assert AiModelsStore(conn).provider_state(h.NS, "epoch-ai")["stale"] is True
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # A 429 answer stops the run and later refreshes wait instead of requesting again.
    limited = [{**p, "status": 429, "headers": {"Retry-After": "3600"}, "body": None} for p in h.pages("hub")]
    stopped = refresh(monitor, "hub", pages=limited, at=h.SECOND_RETRIEVAL)
    assert stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    waiting = refresh(monitor, "hub", at=h.SECOND_RETRIEVAL)
    assert waiting["status"] == "rate_limited_wait" and waiting["units"] == []


def test_watch_targets_are_validated_and_change_kinds_follow_the_revision():
    monitor = AiModelsMonitor(h.connection())
    with pytest.raises(AiModelsError):
        monitor.create(h.NS, "bad", target={"leaderboard": "top"}, principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(AiModelsError):
        monitor.create(h.NS, "bad", target={"source": "kaggle"}, principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(AiModelsError):
        monitor.create(h.NS, "bad", target={"kind": "model"}, principal_id="alice", scopes=h.SCOPES)
    assert change_kinds({"state": "removed_by_source", "changes": {}}) == ["removed_by_source"]
    assert change_kinds({"state": "published", "changes": {"first_revision": True}}) == ["new_revision"]
    assert change_kinds({"state": "published", "changes": {"licence_change": {"before": {}}}}) == [
        "new_revision", "licence_change"]
