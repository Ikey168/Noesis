"""Offline product-to-notices acceptance for the Products safety feature (R11, #2030).

The pinned Safety Gate, CPSC, NHTSA and RASFF fixtures (authored, fictional
products and notice numbers) run through the real source-pack runtime, beside
the pinned Products display fixtures and the ``legal-research`` CELLAR rows for
the cited acts. This is offline evidence only; it is never live provider
coverage (see ``docs/development/product-safety-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.kb.product_safety import NO_NOTICE, ProductSafetyError
from src.kb.product_safety_monitoring import ProductNoticeMonitor
from src.kb.products import readiness
from tests.unit import product_safety_harness as h

NS = h.NS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def updated_pages():
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00417/26")
    page["body"]["alert"]["lastUpdateDate"] = "2026-04-02"
    page["body"]["alert"]["measures"].append(
        {
            "measureType": "Stop of sales",
            "takenBy": "Authorities",
            "category": "Compulsory measures",
        }
    )
    return native


def test_product_gtin_and_brand_model_to_a_cited_notice_dossier():
    env = h.Env().loaded()
    store = env.store
    ex32, ex27 = env.model("icecat", "EX-32U8"), env.model("icecat", "EX-27Q4")
    alert_417, alert_431 = (
        env.notice("safety-gate", "SR/00417/26"),
        env.notice("safety-gate", "SR/00431/26"),
    )
    accepted = env.accept(alert_417, ex32)
    env.accept(alert_431, ex27)
    watcher = ProductNoticeMonitor(env.conn, now=lambda: next(env.clock))
    subscription = watcher.create(
        NS, "acceptance", watch={"models": [ex32]}, principal_id="analyst", scopes=h.ALL
    )["subscription_id"]
    assert [
        n["kind"]
        for n in watcher.run(subscription, principal_id="analyst", scopes=h.ALL)[
            "notifications"
        ]
    ] == ["notice_attached"]

    # An updated alert is a second revision; the first stays inspectable.
    env.run_notices(
        "update",
        adapters={
            "safety-gate-alerts": env.compiled("safety-gate-alerts", updated_pages())
        },
        source_ids=["safety-gate-alerts"],
    )
    store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    store.link_citations(NS, scopes=h.ALL, principal_id="linker")
    revisions = store.revisions(NS, alert_417)
    assert [r["revision_no"] for r in revisions] == [1, 2]
    first_parts = store.parts(NS, revisions[0]["revision_id"])
    assert [a["text"] for a in first_parts["corrective_actions"]] == [
        "Recall of the product from end users"
    ]

    # Journey 1: a Products model id to its cited dossier, as of now and as of a date.
    dossier = store.lookup(NS, scopes=h.READ, model_id=ex32)
    assert dossier["status"] == "notices on record" and not h.forbidden_keys(dossier)
    (notice,) = dossier["notices"]
    assert (notice["notice_number"], notice["revision"]["revision_no"]) == (
        "SR/00417/26",
        2,
    )
    assert notice["connections"][0]["match_id"] == accepted["match_id"]
    assert notice["connections"][0]["decision"]["decision"] == "accepted"
    raw = h.alert_page(h.pages("safety-gate-alerts"), "SR/00417/26")["body"]["alert"]
    assert (
        notice["corrective_actions"][0]["quoted_text"]
        == raw["measures"][0]["measureType"]
    )
    assert notice["hazards"][0]["description"] == raw["risk"]["description"]
    gpsr = next(
        c for c in notice["cited_legal_acts"] if c["raw"] == "Regulation (EU) 2023/988"
    )
    assert (
        gpsr["links"][0]["target_kind"] == "legal-work"
        and gpsr["links"][0]["basis"] == "cited"
    )
    assert notice["cited_standards"][0]["resolution"].startswith(
        "unresolved"
    )  # EN references stay raw
    as_of = store.lookup(NS, scopes=h.READ, model_id=ex32, as_of="2026-03-20")
    assert as_of["notices"][0]["revision"]["revision_no"] == 1
    assert as_of["notices"][0]["later_revisions"][0]["revision_date"] == "2026-04-02"

    # Journey 2: a GTIN, and Journey 3: a brand plus model string.
    by_gtin = store.lookup(NS, scopes=h.READ, gtin="4012345000016")
    assert [n["notice_number"] for n in by_gtin["notices"]] == ["SR/00431/26"]
    assert any(
        m["attached"]
        for m in by_gtin["notices"][0]["connections"][0]["product_matches"]
    )
    by_string = store.lookup(NS, scopes=h.READ, brand="Exampla", designation="EX-32U8")
    assert [n["notice_number"] for n in by_string["notices"]] == ["SR/00417/26"]

    # Two authorities naming the same GTIN, side by side with their own revisions.
    both = store.lookup(NS, scopes=h.READ, gtin="012345678905")
    assert [
        (n["issuing_authority"]["value"], n["notice_number"]) for n in both["notices"]
    ] == [("us-cpsc", "26117"), ("eu-safety-gate", "SR/00388/26")]
    assert len({n["revision"]["revision_id"] for n in both["notices"]}) == 2

    # A GTIN that contradicts the brand cannot be accepted.
    contradicted = store.matches_for_notice(
        NS, env.notice("safety-gate", "SR/00502/26")
    )
    with pytest.raises(ProductSafetyError) as caught:
        store.review_match(
            NS,
            contradicted[0]["match_id"],
            "accepted",
            "try",
            scopes=h.REVIEW,
            principal_id="r",
        )
    assert caught.value.code == "contradicted_match"

    # A sibling of the recalled model has no notice on record, and nothing says it is safe.
    sibling = store.lookup(NS, scopes=h.READ, model_id=env.model("icecat", "EX-32U8UK"))
    assert (
        sibling["status"] == NO_NOTICE
        and sibling["notices"] == []
        and not h.forbidden_keys(sibling)
    )

    # The update was heard, then a review reversal detaches the notice and is heard too.
    revised = watcher.run(subscription, principal_id="analyst", scopes=h.ALL)[
        "notifications"
    ]
    assert [(n["kind"], n["corrective_action_changed"]) for n in revised] == [
        ("notice_revised", True)
    ]
    store.review_match(
        NS,
        accepted["match_id"],
        "rejected",
        "the notice concerns the adapter sold separately",
        scopes=h.REVIEW,
        principal_id="second-reviewer",
    )
    detached = watcher.run(subscription, principal_id="analyst", scopes=h.ALL)[
        "notifications"
    ]
    assert [n["kind"] for n in detached] == ["notice_detached"]
    assert store.lookup(NS, scopes=h.READ, model_id=ex32)["status"] == NO_NOTICE

    # Every other authority's notice is in the store with its native identification.
    nhtsa = store.inspect(NS, "nhtsa:26V104000", scopes=h.READ)
    assert {
        i["value"]
        for i in nhtsa["revision"]["identifications"]
        if i["kind"] == "model_year"
    } == {"2025", "2026"}
    rasff = store.inspect(NS, "rasff:2026.0457", scopes=h.READ)
    assert rasff["revision"]["followups"][0]["date"] == "2026-03-04"
    assert any(
        c["links"]
        for c in rasff["revision"]["citations"]
        if c["raw"] == "ISO 6579-1:2017"
    )


def test_a_crash_between_projection_and_checkpoint_replays_without_duplicate_notices():
    env = h.Env()
    env.run_displays()

    def crash(source_id, page):
        if source_id == "cpsc-recalls" and page == 1:
            raise RuntimeError("crash after projection, before checkpoint")

    with pytest.raises(RuntimeError):
        env.run_notices("crash", fault=crash)
    assert env.run_notices("crash")["status"] == "complete"
    assert env.run_notices("again")["status"] == "complete"
    counts = env.conn.execute(
        "SELECT count(*), count(DISTINCT notice_id) FROM product_safety_revisions"
    ).fetchone()
    assert counts == (8, 8)
    assert (
        env.conn.execute("SELECT count(*) FROM product_safety_notices").fetchone()[0]
        == 8
    )


def test_offline_and_live_evidence_are_reported_separately():
    env = h.Env()
    env.run_notices()
    status = readiness(env.conn, secrets=lambda _name: None)
    notice = status["notice_providers"]
    assert {
        notice[p]["fixture"] for p in ("safety-gate", "cpsc", "nhtsa", "rasff")
    } == {"ready"}
    assert {
        notice[p]["live_verification"]
        for p in ("safety-gate", "cpsc", "nhtsa", "rasff")
    } == {"unverified-live"}
    assert {notice[p]["live"] for p in ("baua", "gpsr")} == {"not-implemented"}
    assert {
        notice[p]["last_run"]["status"]
        for p in ("safety-gate", "cpsc", "nhtsa", "rasff")
    } == {"complete"}
    for path in h.FIXTURES.values():
        assert (
            json.loads(path.read_text())["captured"] is None
        )  # authored offline fixtures, never live coverage
    evidence = h.ROOT / "docs/development/product-safety-evidence"
    assert (evidence / "README.md").exists()
    assert not list(
        evidence.glob("live-check-*.json")
    )  # no dated live run yet (R12, #2033)
    assert "never recorded here" in (evidence / "README.md").read_text()
