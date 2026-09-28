"""Products safety notices: the notice, revision and part records of ``src/kb/product_safety.py`` (R02, #1950)."""

from __future__ import annotations

import copy
import json

import duckdb
import jsonschema
import pytest

from src.ingestion.product_sources import parse_safety_gate_alert
from src.kb.product_safety import (
    CONTRACT,
    ProductSafetyError,
    ProductSafetyStore,
    gtin_key,
)
from tests.unit import product_safety_harness as h

NS = h.NS


@pytest.fixture()
def env():
    item = h.Env()
    yield item
    item.conn.close()


def alert(**changes) -> dict:
    raw = copy.deepcopy(
        h.alert_page(h.pages("safety-gate-alerts"), "SR/00417/26")["body"]["alert"]
    )
    for key, value in changes.items():
        if key == "product":
            raw["product"].update(value)
        else:
            raw[key] = value
    return parse_safety_gate_alert(raw)


def test_statements_follow_the_contract_and_carry_no_verdicts():
    schema = json.loads(
        (
            h.ROOT / "contracts/schemas/jsonschema/noesis-product-safety-notice-v1.json"
        ).read_text()
    )
    statement = alert()
    jsonschema.validate(statement, schema)
    assert statement["contract"] == CONTRACT
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**statement, "risk_score": 0.9}, schema)
    store = ProductSafetyStore(duckdb.connect(":memory:"))
    with pytest.raises(ProductSafetyError) as caught:
        store.apply(NS, {**statement, "verdict": "unsafe"})
    assert caught.value.code == "invalid_record"


def test_a_newer_revision_becomes_current_and_the_superseded_text_stays(env):
    store = env.store
    first = store.apply(NS, alert())
    assert first["status"] == "created" and first["revision_no"] == 1
    updated = alert(
        lastUpdateDate="2026-04-02",
        measures=[
            {
                "measureType": "Recall of the product from end users",
                "takenBy": "Economic operator",
            },
            {"measureType": "Stop of sales", "takenBy": "Authorities"},
        ],
    )
    second = store.apply(NS, updated)
    assert second["status"] == "revised" and second["revision_no"] == 2
    notice_id = first["notice_id"]
    current, _ = store.revision_as_of(NS, notice_id, None)
    assert (
        current["revision_id"] == second["revision_id"]
        and current["revision_date"] == "2026-04-02"
    )
    old = store.parts(NS, first["revision_id"])
    assert [a["text"] for a in old["corrective_actions"]] == [
        "Recall of the product from end users"
    ]
    assert len(store.parts(NS, second["revision_id"])["corrective_actions"]) == 2


def test_an_older_revision_delivered_later_never_replaces_the_current_one(env):
    store = env.store
    newer = store.apply(
        NS,
        alert(
            lastUpdateDate="2026-04-02",
            risk={
                "riskType": ["Electric shock"],
                "level": "Serious risk",
                "description": "Updated description.",
            },
        ),
    )
    older = store.apply(NS, alert())
    assert older["status"] == "history" and older["revision_no"] == 2
    current, _ = store.revision_as_of(NS, newer["notice_id"], None)
    assert current["revision_id"] == newer["revision_id"]
    assert [r["revision_date"] for r in store.revisions(NS, newer["notice_id"])] == [
        "2026-04-02",
        "2026-03-13",
    ]
    # The late older payload is kept and can be read as of its own date.
    as_of, later = store.revision_as_of(
        NS, newer["notice_id"], __import__("datetime").date(2026, 3, 20)
    )
    assert as_of["revision_id"] == older["revision_id"] and [
        r["revision_id"] for r in later
    ] == [newer["revision_id"]]
    # Replaying it again adds nothing.
    assert store.apply(NS, alert())["status"] == "unchanged"


def test_replays_add_nothing_and_a_reversion_is_a_new_revision(env):
    store = env.store
    base = store.apply(NS, alert())
    assert store.apply(NS, alert())["status"] == "unchanged"
    corrected = store.apply(NS, alert(product={"batchNumber": "B2025-12"}))
    assert (
        corrected["status"] == "revised"
    )  # same date, corrected content: a correction, not a conflict
    reverted = store.apply(NS, alert())
    assert reverted["status"] == "reverted" and reverted["revision_no"] == 3
    current, _ = store.revision_as_of(NS, base["notice_id"], None)
    assert current["revision_id"] == reverted["revision_id"] != base["revision_id"]


def test_identification_is_verbatim_and_never_normalised_into_a_product(env):
    store = env.store
    result = store.apply(
        NS, alert(product={"barcode": "4012345000029", "batchNumber": "B2025-11 / L7"})
    )
    parts = store.parts(NS, result["revision_id"])
    values = {(i["kind"], i["value"]): i for i in parts["identifications"]}
    assert values[("gtin", "4012345000029")]["gtin_state"] == "invalid_checksum"
    assert values[("batch", "B2025-11 / L7")]["locator"] == {
        "json_pointer": "/product/batchNumber"
    }
    assert values[("model", "EX-32U8")]["locator"] == {
        "json_pointer": "/product/typeNumberOfModel"
    }
    assert not env.conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='product_identities'"
    ).fetchone()[0]
    assert gtin_key("012345678905") == gtin_key("0012345678905") == "00012345678905"
    assert gtin_key("4012345000029") is None


def test_enumerations_keep_the_raw_value_and_unknown_stays_unknown(env):
    result = env.store.apply(NS, alert(notificationType="Special notification"))
    revision = env.store.revisions(NS, result["notice_id"])[0]
    assert (revision["notice_type"], revision["notice_type_declared"]) == (
        "unknown",
        "Special notification",
    )
    assert (revision["authority"], revision["authority_declared"]) == (
        "eu-safety-gate",
        "European Commission (Safety Gate)",
    )
    assert (revision["notifying_country"], revision["notifying_country_declared"]) == (
        "DE",
        "Germany",
    )


def test_reads_require_products_scopes_and_namespace_access(env):
    result = env.store.apply(NS, alert())
    with pytest.raises(ProductSafetyError) as caught:
        env.store.inspect(NS, result["notice_id"], scopes={"knowledge:products:read"})
    assert caught.value.code == "unauthorized"
    with pytest.raises(ProductSafetyError):
        env.store.inspect(NS, result["notice_id"], scopes={"namespace:global:read"})
    with pytest.raises(ProductSafetyError) as caught:
        env.store.inspect(
            "other",
            result["notice_id"],
            scopes={"knowledge:products:read", "namespace:other:read"},
        )
    assert caught.value.code == "not_found"
    view = env.store.inspect(NS, "safety-gate:SR/00417/26", scopes=h.READ)
    assert (
        view["notice_number"] == "SR/00417/26" and view["revision"]["revision_no"] == 1
    )
    with pytest.raises(ProductSafetyError) as caught:
        env.store.propose_matches(NS, scopes=h.READ, principal_id="x")
    assert caught.value.code == "unauthorized"


def test_runtime_projection_is_idempotent_and_a_crash_replays_without_duplicates():
    env = h.Env()
    receipt = env.run_notices("n-1")
    assert receipt["status"] == "complete"
    counts = env.conn.execute(
        "SELECT count(*) FROM product_safety_revisions"
    ).fetchone()[0]
    assert (
        counts == 8
    )  # four Safety Gate alerts (one also in the weekly report), two CPSC, one NHTSA, one RASFF
    assert env.run_notices("n-2")["status"] == "complete"
    assert (
        env.conn.execute("SELECT count(*) FROM product_safety_revisions").fetchone()[0]
        == counts
    )

    crashed = h.Env()

    def fault(source_id, page):
        if source_id == "safety-gate-alerts" and page == 2:
            raise RuntimeError("crash after projection, before checkpoint")

    with pytest.raises(RuntimeError):
        crashed.run_notices("crash", source_ids=["safety-gate-alerts"], fault=fault)
    assert (
        crashed.run_notices("crash", source_ids=["safety-gate-alerts"])["status"]
        == "complete"
    )
    assert (
        crashed.conn.execute(
            "SELECT count(*) FROM product_safety_revisions"
        ).fetchone()[0]
        == 4
    )
    assert (
        crashed.conn.execute(
            "SELECT count(DISTINCT notice_id) FROM product_safety_notices"
        ).fetchone()[0]
        == 4
    )


def test_selection_outcomes_and_source_runs_are_recorded(env):
    receipt = env.run_notices("n-1")
    outcomes = env.store.selection_outcomes(NS, receipt["run_id"])
    assert {(o["source_id"], o["outcome"]) for o in outcomes} >= {
        ("safety-gate-alerts", "not_found"),
        ("rasff-notifications", "returned"),
    }
    runs = env.store.sources_consulted(NS)
    assert {r["source_id"]: r["last_run_status"] for r in runs} == {
        s: "complete" for s in h.NOTICE_SOURCES
    }


def test_reads_before_any_notice_run_say_so_on_a_read_only_store(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    conn = duckdb.connect(path, read_only=True)
    store = ProductSafetyStore(conn, initialize=False)
    for call in (
        lambda: store.lookup(NS, scopes=h.READ, gtin="012345678905"),
        lambda: store.inspect(NS, "cpsc:26117", scopes=h.READ),
    ):
        with pytest.raises(ProductSafetyError) as caught:
            call()
        assert caught.value.code == "not_ready"
    conn.close()
