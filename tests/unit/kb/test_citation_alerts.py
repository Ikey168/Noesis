import pytest

from src.kb.citation_alerts import CATEGORIES, CitationAlertStore
from src.kb.subscriptions import SubscriptionError, SubscriptionStore
from tests.unit.kb.test_report_updates import AUTH as REPORT_AUTH
from tests.unit.kb.test_report_updates import setup

AUTH = {
    **REPORT_AUTH,
    "scopes": REPORT_AUTH["scopes"]
    | {
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
        "namespace:r:read",
    },
}


def prepare():
    reports, sources, payload, report = setup()
    store = CitationAlertStore(reports.conn)
    target = {
        "kind": "report",
        "namespace": "r",
        "id": report["report_id"],
        "revision": 1,
    }
    sub = store.create(target, "watch", sorted(CATEGORIES), 10, **AUTH)
    return store, reports, sources, payload, report, sub


def test_correction_acknowledgment_pause_unrelated_change_and_replay():
    store, reports, sources, payload, report, sub = prepare()
    identity = sub["subscription_id"]
    store.evaluate(identity, **AUTH)
    initial = store.poll(identity, **AUTH)
    assert initial["events"] == []
    sources.observe({**payload, "content": "The corrected value decreased."})
    result = store.evaluate(identity, **AUTH)
    assert result["events"] == 1
    assert store.evaluate(identity, **AUTH)["status"] == "replayed"
    alerts = store.poll(identity, cursor=initial["cursor"], **AUTH)
    event = alerts["events"][0]
    assert event["category"] == "revised"
    assert (
        event["after"]["before_revision"]["revision_id"]
        != event["after"]["after_revision"]["revision_id"]
    )
    assert event["after"]["affected"] == [
        {"section_id": "summary", "assertion_id": "a1"}
    ]
    assert event["actions"][-1]["tool"] == "assess_authored_report_changes"
    assert "payload_json" not in str(event)
    ack = store.acknowledge(identity, event["event_id"], **AUTH)
    assert (
        ack["acknowledged"]
        and not ack["report_modified"]
        and not ack["evidence_approved"]
    )
    assert store.poll(identity, cursor=initial["cursor"], **AUTH)["events"][0][
        "acknowledged"
    ]
    assert reports.inspect("r", report["report_id"], **REPORT_AUTH)["revision"] == 1
    sources.observe({"document_id": "unrelated", "content": "Another page"})
    assert store.evaluate(identity, **AUTH)["status"] == "replayed"
    store.set_status(identity, "paused", **AUTH)
    assert store.evaluate(identity, **AUTH)["reason"] == "paused"
    store.set_status(identity, "active", **AUTH)
    assert store.evaluate(identity, **AUTH)["status"] == "replayed"
    store.set_status(identity, "deleted", **AUTH)
    assert store.evaluate(identity, **AUTH)["reason"] == "deleted"


def test_pending_fetch_is_unavailability_not_withdrawal_then_recovery():
    store, _, sources, payload, _, sub = prepare()
    identity = sub["subscription_id"]
    store.evaluate(identity, **AUTH)
    cursor = store.poll(identity, **AUTH)["cursor"]
    pending = sources.observe(
        {
            **payload,
            "content": "Pending content",
            "metadata": {"source_pack_id": "pack", "source_pack_run_id": "pending"},
        }
    )
    store.evaluate(identity, **AUTH)
    event = store.poll(identity, cursor=cursor, **AUTH)["events"][0]
    assert event["category"] == "unavailable"
    cursor = store.poll(identity, cursor=cursor, **AUTH)["cursor"]
    store.conn.execute(
        "UPDATE document_revision_records SET committed_watermark=1 WHERE revision_id=?",
        [pending["revision_id"]],
    )
    store.evaluate(identity, **AUTH)
    assert (
        store.poll(identity, cursor=cursor, **AUTH)["events"][0]["category"]
        == "recovered"
    )
    sources.observe({**payload, "metadata": {"lifecycle": "retracted"}})
    store.evaluate(identity, **AUTH)
    assert (
        store.poll(identity, cursor=cursor, **AUTH)["events"][-1]["category"]
        == "withdrawn"
    )


def test_revocation_blocks_generic_and_specialized_delivery():
    store, _, sources, payload, _, sub = prepare()
    identity = sub["subscription_id"]
    sources.observe(
        {**payload, "content": "The reported value has decreased substantially."}
    )
    store.evaluate(identity, **AUTH)
    revoked = {**AUTH, "scopes": AUTH["scopes"] - {"document:doc:read"}}
    for reader in [store, SubscriptionStore(store.conn, initialize=False)]:
        with pytest.raises((SubscriptionError, ValueError)) as exc:
            reader.poll(identity, **revoked)
        assert exc.value.code == "unauthorized"
    with pytest.raises(SubscriptionError):
        store.acknowledge(identity, "wrong-event", **AUTH)


def test_project_scope_and_multiple_reports_have_independent_alerts():
    store, _reports, sources, payload, report, sub = prepare()
    from src.kb.research_projects import ResearchProjectStore

    auth = {"principal_id": "alice", "scopes": {"operator"}}
    projects = ResearchProjectStore(store.conn)
    p = projects.create(
        "r",
        "project",
        questions=["Why?"],
        success_criteria=["Cite"],
        scope={"namespaces": ["r"], "domains": []},
        budget={},
        **auth,
    )
    dep = report["content"]["sections"][0]["assertions"][0]["dependencies"][0]
    p = projects.revise(
        "r",
        p["project_id"],
        1,
        add_links=[
            {
                "kind": "evidence",
                "id": "claim",
                "revision": 1,
                "locator": dep["locator"],
            }
        ],
        **auth,
    )
    project_sub = store.create(
        {
            "kind": "project",
            "namespace": "r",
            "id": p["project_id"],
            "revision": p["revision"],
        },
        "project-watch",
        ["revised"],
        1,
        **auth,
    )
    sources.observe(
        {**payload, "content": "The reported value has decreased substantially."}
    )
    for identity, credentials in [
        (sub["subscription_id"], AUTH),
        (project_sub["subscription_id"], auth),
    ]:
        store.evaluate(identity, **credentials)
        assert store.poll(identity, **credentials)["events"][0]["category"] == "revised"


def test_inferred_takedown_remains_a_review_notice():
    store, _, sources, payload, _, sub = prepare()
    # The legacy source change classifier treats severe text loss as a takedown;
    # this is not explicit provider lifecycle confirmation.
    sources.observe({**payload, "content": "Changed"})
    store.evaluate(sub["subscription_id"], **AUTH)
    event = store.poll(sub["subscription_id"], **AUTH)["events"][0]
    assert event["category"] == "notice"
    assert event["after"]["reason"] == "inferred_withdrawal_requires_review"
