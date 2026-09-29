"""Regressions for the Products safety code review (NHTSA empty fields, as-of naming, equivalents, cutoffs, readiness)."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.ingestion.product_sources import parse_nhtsa_campaign
from src.kb.product_safety import ProductSafetyError, ProductSafetyStore
from src.kb.product_safety_monitoring import ProductNoticeMonitor
from src.kb.products import ProductStore
from tests.unit import product_safety_harness as h

NS = h.NS


@pytest.fixture()
def env():
    item = h.Env().loaded()
    yield item
    item.conn.close()


def monitor(env) -> ProductNoticeMonitor:
    return ProductNoticeMonitor(env.conn, now=lambda: next(env.clock))


def test_an_nhtsa_campaign_without_remedy_or_manufacturer_stores_neither():
    rows = copy.deepcopy(h.pages("nhtsa-recalls")[0]["body"]["results"])
    for row in rows:
        row.update({"Remedy": None, "Manufacturer": ""})
    statement = parse_nhtsa_campaign(rows)
    assert statement["corrective_actions"] == [] and statement["parties"] == []
    store = ProductSafetyStore(duckdb.connect(":memory:"))
    parts = store.parts(NS, store.apply(NS, statement)["revision_id"])
    assert parts["corrective_actions"] == [] and parts["parties"] == []
    assert "None" not in json.dumps(parts)


def test_an_as_of_model_lookup_needs_the_as_of_revision_to_name_the_product(env):
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00388/26")
    page["body"]["alert"]["lastUpdateDate"] = "2026-04-02"
    page["body"]["alert"]["product"].update(
        {"brand": "Exampla", "typeNumberOfModel": "EX-34Q4", "barcode": None}
    )
    env.run_notices(
        "renamed",
        adapters={"safety-gate-alerts": env.compiled("safety-gate-alerts", native)},
        source_ids=["safety-gate-alerts"],
    )
    env.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    model = env.model("icecat", "EX-34Q4")
    env.accept(env.notice("safety-gate", "SR/00388/26"), model)
    now = env.store.lookup(NS, scopes=h.READ, model_id=model)
    assert [n["notice_number"] for n in now["notices"]] == ["SR/00388/26"]
    before = env.store.lookup(NS, scopes=h.READ, model_id=model, as_of="2026-03-15")
    assert before["status"] == "no notice on record" and before["notices"] == []
    assert before["later_notices"][0]["notice_id"] == env.notice(
        "safety-gate", "SR/00388/26"
    )
    assert "later revision" in before["later_notices"][0]["note"]


def test_a_monitor_on_a_model_hears_notices_attached_to_its_accepted_equivalent(env):
    products = ProductStore(env.conn, initialize=False)
    icecat, eprel = env.model("icecat", "EX-32U8"), env.model("eprel", "EX-32U8")
    candidate = next(
        m
        for m in products.propose_matches(
            NS, scopes=h.REVIEW | {"knowledge:products:write"}, principal_id="m"
        )["candidates"]
        if {m["left_model_id"], m["right_model_id"]} == {icecat, eprel}
    )
    products.review_match(
        NS,
        candidate["match_id"],
        "accepted",
        "same model",
        scopes=h.REVIEW,
        principal_id="r",
    )
    env.accept(env.notice("safety-gate", "SR/00417/26"), icecat)
    watcher = monitor(env)
    subscription = watcher.create(
        NS,
        "equivalent",
        watch={"models": [eprel]},
        principal_id="analyst",
        scopes=h.ALL,
    )["subscription_id"]
    heard = watcher.run(subscription, principal_id="analyst", scopes=h.ALL)[
        "notifications"
    ]
    lookup = env.store.lookup(NS, scopes=h.READ, model_id=eprel)
    assert [n["notice_number"] for n in lookup["notices"]] == ["SR/00417/26"]
    assert [(n["kind"], n["cites"]["notice_number"]) for n in heard] == [
        ("notice_attached", "SR/00417/26")
    ]


def test_monitor_match_state_stops_at_the_complete_run_cutoff(env):
    from src.ingestion.product_sources import fixture_transport

    watcher = monitor(env)
    model = env.model("icecat", "EX-32U8")
    subscription = watcher.create(
        NS, "cutoff", watch={"models": [model]}, principal_id="analyst", scopes=h.ALL
    )["subscription_id"]
    assert (
        watcher.run(subscription, principal_id="analyst", scopes=h.ALL)["notifications"]
        == []
    )
    native = h.pages("safety-gate-alerts")
    h.alert_page(native, "SR/00417/26")["body"]["alert"]["lastUpdateDate"] = (
        "2026-04-02"
    )
    failing = [dict(p, status=503, body=None) for p in h.pages("cpsc-recalls")]
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    env.run_notices(
        "partial",
        source_ids=["safety-gate-alerts", "cpsc-recalls"],
        adapters={
            "safety-gate-alerts": env.compiled("safety-gate-alerts", native),
            "cpsc-recalls": env.runtime.factory.compile(
                h.source("cpsc-recalls", installed),
                transport=fixture_transport(failing),
            ),
        },
    )
    env.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    accepted = env.accept(env.notice("safety-gate", "SR/00417/26"), model)
    later = env.store.revisions(NS, env.notice("safety-gate", "SR/00417/26"))[-1]
    assert (
        accepted["revision_id"] == later["revision_id"]
    )  # reviewed against the partial run's revision
    held = watcher.run(subscription, principal_id="analyst", scopes=h.ALL)
    assert held["notifications"] == []  # nothing past the last complete run is cited
    env.run_notices(
        "complete",
        source_ids=["safety-gate-alerts", "cpsc-recalls"],
        adapters={"safety-gate-alerts": env.compiled("safety-gate-alerts", native)}
        | {"cpsc-recalls": env.compiled("cpsc-recalls", h.pages("cpsc-recalls"))},
    )
    heard = watcher.run(subscription, principal_id="analyst", scopes=h.ALL)[
        "notifications"
    ]
    assert [(n["kind"], n["cites"]["revision_id"]) for n in heard] == [
        ("notice_attached", later["revision_id"])
    ]


def test_every_read_entry_point_reports_not_ready_before_any_notice_run(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    conn = duckdb.connect(path, read_only=True)
    store = ProductSafetyStore(conn, initialize=False)
    calls = [
        lambda: store.propose_party_links(NS, scopes=h.READ),
        lambda: store.selection_outcomes(NS, "run-1"),
        lambda: store.sources_consulted(NS),
        lambda: store.matches_for_notice(NS, "product-safety-notice:x"),
        lambda: store.lookup(NS, scopes=h.READ, gtin="012345678905"),
        lambda: store.inspect(NS, "cpsc:26117", scopes=h.READ),
        lambda: ProductNoticeMonitor(conn, initialize=False).poll(
            "sub-1", principal_id="a", scopes=h.ALL
        ),
    ]
    for call in calls:
        with pytest.raises(ProductSafetyError) as caught:
            call()
        assert caught.value.code == "not_ready"
    conn.close()
