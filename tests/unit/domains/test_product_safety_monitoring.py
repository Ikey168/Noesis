"""Products safety notices: monitors on the subscription store (R09, #2020)."""

from __future__ import annotations

import pytest

from src.ingestion.product_sources import fixture_transport
from src.kb.product_safety import ProductSafetyError
from src.kb.product_safety_monitoring import ProductNoticeMonitor
from tests.unit import product_safety_harness as h

NS = h.NS
SCOPES = h.ALL


@pytest.fixture()
def env():
    item = h.Env().loaded()
    yield item
    item.conn.close()


def monitor(env) -> ProductNoticeMonitor:
    return ProductNoticeMonitor(env.conn, now=lambda: next(env.clock))


def updated_alert_pages(date="2026-04-02", measure="Stop of sales"):
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00417/26")
    page["body"]["alert"]["lastUpdateDate"] = date
    page["body"]["alert"]["measures"].append(
        {"measureType": measure, "takenBy": "Authorities"}
    )
    return native


def kinds(result) -> list[tuple[str, str]]:
    return sorted(
        (n["kind"], n["cites"]["notice_number"]) for n in result["notifications"]
    )


def test_a_watched_model_hears_attachment_revisions_and_a_reversal(env):
    watcher = monitor(env)
    model = env.model("icecat", "EX-32U8")
    created = watcher.create(
        NS, "m-1", watch={"models": [model]}, principal_id="analyst", scopes=SCOPES
    )
    subscription = created["subscription_id"]
    assert (
        watcher.run(subscription, principal_id="analyst", scopes=SCOPES)[
            "notifications"
        ]
        == []
    )

    accepted = env.accept(env.notice("safety-gate", "SR/00417/26"), model)
    attached = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert kinds(attached) == [("notice_attached", "SR/00417/26")]
    note = attached["notifications"][0]
    assert (
        note["match"]["match_id"] == accepted["match_id"]
        and note["match"]["decision"] == "accepted"
    )
    assert (
        note["cites"]["revision_no"] == 1
        and note["cites"]["authority"] == "eu-safety-gate"
    )
    assert (
        watcher.run(subscription, principal_id="analyst", scopes=SCOPES)[
            "notifications"
        ]
        == []
    )

    env.run_notices(
        "update",
        adapters={
            "safety-gate-alerts": env.compiled(
                "safety-gate-alerts", updated_alert_pages()
            )
        },
        source_ids=["safety-gate-alerts"],
    )
    env.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    revised = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert kinds(revised) == [("notice_revised", "SR/00417/26")]
    assert revised["notifications"][0]["corrective_action_changed"] is True
    assert revised["notifications"][0]["hazard_changed"] is False

    env.store.review_match(
        NS,
        accepted["match_id"],
        "rejected",
        "adapter only",
        scopes=h.REVIEW,
        principal_id="reviewer",
    )
    detached = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert kinds(detached) == [("notice_detached", "SR/00417/26")]
    assert detached["notifications"][0]["match"]["decision"] == "rejected"
    assert (
        detached["source_watermark"] == revised["source_watermark"]
    )  # a review alone is evaluated


def test_a_sibling_of_a_watched_model_hears_nothing(env):
    watcher = monitor(env)
    sibling = env.model("icecat", "EX-32U8UK")
    subscription = watcher.create(
        NS, "sib", watch={"models": [sibling]}, principal_id="analyst", scopes=SCOPES
    )["subscription_id"]
    env.accept(env.notice("safety-gate", "SR/00417/26"), env.model("icecat", "EX-32U8"))
    assert (
        watcher.run(subscription, principal_id="analyst", scopes=SCOPES)[
            "notifications"
        ]
        == []
    )


def test_a_watched_brand_hears_brand_level_events_marked_by_identification(env):
    watcher = monitor(env)
    env.accept(env.notice("safety-gate", "SR/00431/26"), env.model("icecat", "EX-27Q4"))
    subscription = watcher.create(
        NS,
        "brand",
        watch={"brands": ["exampla"]},
        principal_id="analyst",
        scopes=SCOPES,
    )["subscription_id"]
    result = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    events = {n["cites"]["notice_number"]: n for n in result["notifications"]}
    assert set(events) == {"SR/00417/26", "SR/00431/26"}  # CPSC 26140 states no brand
    assert events["SR/00417/26"]["identification"] == "unmatched identification"
    assert events["SR/00431/26"]["identification"] == "matched to a product"
    assert all(n["kind"] == "new_notice" for n in result["notifications"])


def test_authority_and_gtin_watches(env):
    watcher = monitor(env)
    by_authority = watcher.create(
        NS,
        "nhtsa",
        watch={"authorities": ["us-nhtsa"]},
        principal_id="analyst",
        scopes=SCOPES,
    )["subscription_id"]
    assert kinds(watcher.run(by_authority, principal_id="analyst", scopes=SCOPES)) == [
        ("new_notice", "26V104000")
    ]
    by_gtin = watcher.create(
        NS,
        "gtin",
        watch={"gtins": ["012345678905"]},
        principal_id="analyst",
        scopes=SCOPES,
    )["subscription_id"]
    assert kinds(watcher.run(by_gtin, principal_id="analyst", scopes=SCOPES)) == [
        ("new_notice", "26117"),
        ("new_notice", "SR/00388/26"),
    ]
    with pytest.raises(ProductSafetyError):
        watcher.create(
            NS,
            "bad",
            watch={"authorities": ["eu-somewhere"]},
            principal_id="analyst",
            scopes=SCOPES,
        )
    with pytest.raises(ProductSafetyError):
        watcher.create(
            NS,
            "bad2",
            watch={"models": ["product-model:none"]},
            principal_id="analyst",
            scopes=SCOPES,
        )


def test_a_partial_run_is_never_evaluated_and_leaves_no_silent_gap(env):
    watcher = monitor(env)
    model = env.model("icecat", "EX-32U8")
    env.accept(env.notice("safety-gate", "SR/00417/26"), model)
    subscription = watcher.create(
        NS, "gap", watch={"models": [model]}, principal_id="analyst", scopes=SCOPES
    )["subscription_id"]
    first = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert kinds(first) == [("notice_attached", "SR/00417/26")]
    failing = h.pages("cpsc-recalls")
    for page in failing:
        page.update({"status": 503, "body": None})
    partial = env.run_notices(
        "partial",
        source_ids=["safety-gate-alerts", "cpsc-recalls"],
        adapters={
            "safety-gate-alerts": env.compiled(
                "safety-gate-alerts", updated_alert_pages()
            ),
            "cpsc-recalls": env.runtime.factory.compile(
                h.source(
                    "cpsc-recalls", env.runtime._manifest(env.value["pack_id"])[0]
                ),
                transport=fixture_transport(failing),
            ),
        },
    )
    assert {s["source_id"]: s["status"] for s in partial["sources"]}[
        "cpsc-recalls"
    ] == "failed"
    assert partial["watermark"] is not None
    skipped = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert (
        skipped["source_watermark"] == first["source_watermark"]
        and skipped["notifications"] == []
    )
    with pytest.raises(ProductSafetyError) as caught:
        watcher.run(
            subscription, partial["watermark"], principal_id="analyst", scopes=SCOPES
        )
    assert caught.value.code == "incomplete_run"
    env.run_notices("complete-again", source_ids=["safety-gate-alerts", "cpsc-recalls"])
    caught_up = watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    assert kinds(caught_up) == [("notice_revised", "SR/00417/26")]


def test_a_late_older_payload_is_not_a_revision_event(env):
    watcher = monitor(env)
    model = env.model("icecat", "EX-32U8")
    env.accept(env.notice("safety-gate", "SR/00417/26"), model)
    subscription = watcher.create(
        NS, "late", watch={"models": [model]}, principal_id="analyst", scopes=SCOPES
    )["subscription_id"]
    watcher.run(subscription, principal_id="analyst", scopes=SCOPES)
    older = h.pages("safety-gate-alerts")
    page = h.alert_page(older, "SR/00417/26")
    page["body"]["alert"]["publicationDate"] = "2026-03-01"
    page["body"]["alert"]["risk"]["description"] = "An earlier wording."
    env.run_notices(
        "older",
        source_ids=["safety-gate-alerts"],
        adapters={"safety-gate-alerts": env.compiled("safety-gate-alerts", older)},
    )
    assert len(env.store.revisions(NS, env.notice("safety-gate", "SR/00417/26"))) == 2
    assert (
        watcher.run(subscription, principal_id="analyst", scopes=SCOPES)[
            "notifications"
        ]
        == []
    )


def test_delivery_uses_the_configured_channel(env):
    watcher = monitor(env)
    subscription = watcher.create(
        NS,
        "hook",
        watch={"authorities": ["eu-rasff"]},
        principal_id="analyst",
        scopes=SCOPES,
        delivery={"kind": "webhook", "destination_ref": "hook:ops"},
    )
    result = watcher.run(
        subscription["subscription_id"], principal_id="analyst", scopes=SCOPES
    )
    assert (
        kinds(result) == [("new_notice", "2026.0457")]
        and result["delivery"]["kind"] == "webhook"
    )
    outbox = env.conn.execute(
        "SELECT count(*) FROM knowledge_subscription_outbox"
    ).fetchone()[0]
    assert outbox == 1
    polled = watcher.poll(
        subscription["subscription_id"], principal_id="analyst", scopes=SCOPES
    )
    assert polled
