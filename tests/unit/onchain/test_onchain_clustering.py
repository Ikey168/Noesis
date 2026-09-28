"""Probable address clustering, calibration and null model (#2057)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb import onchain_calibration as cal
from src.kb.onchain import OnchainStore
from src.kb.onchain_clustering import HEURISTICS, NOTE, address_cluster, coinjoin_like
from tests.unit.onchain import fixture_builder as fb

FORBIDDEN_KEYS = {
    "owner",
    "owners",
    "owned_by",
    "controller",
    "controlled_by",
    "attribution_verdict",
    "entity",
    "person",
    "identity",
    "attributed_to",
}


@pytest.fixture(scope="module")
def journey():
    conn = duckdb.connect(":memory:")
    for source in fb.RAW_FILES:
        fb.run_journey(conn, source)
    yield conn
    conn.close()


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_deposit_reuse_cluster_is_probable_cited_and_has_no_owner_field(journey):
    result = address_cluster(journey, fb.NAMESPACE, "eip155:1", fb.ETH["journey-user"])
    assert result["status"] == "probable"
    assert [m["address"] for m in result["members"]] == [
        fb.lower("journey-user-second")
    ]
    edge = result["edges"][0]
    assert edge["heuristic"] == "deposit-address-reuse" and edge["status"] == "probable"
    assert edge["hub"]["id"] == fb.lower("journey-deposit") and edge["hub"][
        "sweeps_to"
    ] == fb.lower("journey-exchange")
    cited = {e["tx_hash"] for e in edge["evidence"]}
    assert {
        fb.TX["user-to-deposit"],
        fb.TX["second-to-deposit"],
        fb.TX["deposit-sweep"],
    } <= cited
    assert all(
        e["citation"]["cited"]
        and e["citation"]["explorer_url"].startswith("https://etherscan.io/tx/")
        for e in edge["evidence"]
    )
    assert (
        result["note"] == NOTE
        and "exchange, bridge and mixer" in result["note"].lower()
    )
    assert set(result["heuristics"]) == set(HEURISTICS)
    assert result["threshold"]["min_score"] == cal.DEFAULT_MIN_SCORE
    measured = result["threshold"]["calibration"]["measured_at_default"]
    assert measured["fpr"] <= cal.TARGET_FPR
    assert {"n", "method", "assumptions"} <= set(result)
    assert not FORBIDDEN_KEYS & set(_keys(result))


def test_common_input_cluster_on_bitcoin(journey):
    result = address_cluster(journey, fb.NAMESPACE, "bitcoin", fb.BTC["wallet-one"])
    assert result["status"] == "probable"
    assert [(e["heuristic"], e["hub"]["id"]) for e in result["edges"]] == [
        ("common-input", fb.BTX["cospend"])
    ]
    assert [m["address"] for m in result["members"]] == [fb.BTC["wallet-two"]]


def test_refuses_without_a_cited_transaction_and_before_acquisition(journey):
    exchange = address_cluster(
        journey, fb.NAMESPACE, "eip155:1", fb.ETH["journey-exchange"]
    )
    assert exchange["status"] == "no_probable_cluster"  # receiving a sweep links nobody
    stranger = fb.ETH["first-buyer"]
    empty = duckdb.connect(":memory:")
    assert (
        address_cluster(empty, fb.NAMESPACE, "eip155:1", stranger)["status"]
        == "not_ready"
    )
    OnchainStore(empty)
    result = address_cluster(empty, fb.NAMESPACE, "eip155:1", stranger)
    assert result["status"] == "no_cited_transaction" and result["members"] == []
    assert (
        address_cluster(empty, fb.NAMESPACE, "eip155:1", "exampla.eth")["code"]
        == "name_lookup_refused"
    )
    assert (
        address_cluster(journey, fb.NAMESPACE, "eip155:1", stranger, min_score=0)[
            "code"
        ]
        == "invalid_threshold"
    )


def test_traversal_is_bounded():
    scenario = next(
        s for s in cal.load_fixture()["scenarios"] if s["name"] == "wallet-cospend"
    )
    conn, chain = cal.build_ledger(scenario)
    try:
        start = cal._address(chain, "w1")
        shallow = address_cluster(
            conn, "calibration", chain, start, max_depth=1, report_calibration=False
        )
        deep = address_cluster(
            conn, "calibration", chain, start, max_depth=9, report_calibration=False
        )
        assert {m["depth"] for m in shallow["members"]} == {1}
        assert (
            deep["bounds"]["max_depth"] == 3
            and max(m["depth"] for m in deep["members"]) == 2
        )
        tight = address_cluster(
            conn, "calibration", chain, start, max_edges=1, report_calibration=False
        )
        assert tight["truncated"] and len(tight["edges"]) == 1
    finally:
        conn.close()


def test_hot_wallets_bridges_and_coinjoins_stay_out_at_the_served_default():
    for name, probe in (
        ("exchange-hot-wallet", "h1"),
        ("bridge-contract", "b1"),
        ("coinjoin", "cj1"),
        ("exchange-funded-deployers", "d2"),
    ):
        scenario = next(s for s in cal.load_fixture()["scenarios"] if s["name"] == name)
        conn, chain = cal.build_ledger(scenario)
        try:
            result = address_cluster(
                conn,
                "calibration",
                chain,
                cal._address(chain, probe),
                report_calibration=False,
            )
            assert result["members"] == [], name
            assert result["below_threshold"] or result["excluded"], name
        finally:
            conn.close()


def test_coinjoin_filter():
    evm_like = {
        "inputs": [{"address": f"a{i}"} for i in range(5)],
        "outputs": [{"value": 10}] * 5,
    }
    assert coinjoin_like(evm_like)
    assert not coinjoin_like(
        {"inputs": [{"address": "a"}, {"address": "b"}], "outputs": [{"value": 1}] * 3}
    )


def test_calibration_reruns_and_the_served_default_is_the_smallest_within_target_threshold():
    result = cal.calibrate()
    assert result["recommended"]["min_score"] == cal.DEFAULT_MIN_SCORE
    rows = result["levels"]
    within = [
        r["min_score"]
        for r in rows
        if r["fpr"] <= cal.TARGET_FPR and r["tpr"] >= cal.MIN_TPR
    ]
    assert within and min(within) == cal.DEFAULT_MIN_SCORE
    lower = [r for r in rows if r["min_score"] < cal.DEFAULT_MIN_SCORE]
    assert lower and all(
        r["fpr"] > cal.TARGET_FPR for r in lower
    )  # the known false positives appear below it
    served = next(r for r in rows if r["min_score"] == cal.DEFAULT_MIN_SCORE)
    assert served["coincidental_pairs"] >= 10 and served["related_pairs"] >= 10


def test_fixture_includes_the_known_false_positive_cases_and_fictional_addresses():
    fixture = cal.load_fixture()
    names = {s["name"] for s in fixture["scenarios"]}
    assert {
        "exchange-hot-wallet",
        "bridge-contract",
        "coinjoin",
        "exchange-funded-deployers",
    } <= names
    assert "fictional" in fixture["description"]
    assert not any(
        ch in json.dumps(fixture) for ch in ("0x", "bc1")
    )  # labels only; addresses are derived


def test_null_model_is_deterministic_and_reported():
    scenario = next(
        s for s in cal.load_fixture()["scenarios"] if s["name"] == "wallet-cospend"
    )
    conn, chain = cal.build_ledger(scenario)
    try:
        start = cal._address(chain, "w2")
        first = address_cluster(
            conn, "calibration", chain, start, report_calibration=False
        )["null_model"]
        second = address_cluster(
            conn, "calibration", chain, start, report_calibration=False
        )["null_model"]
        assert first == second and first["method"] == "degree-preserving hub shuffle"
        assert first["permutations"] > 0 and first["expected_neighbours"] is not None
        assert first["observed_neighbours"] == 3
    finally:
        conn.close()


def test_monitors_and_lookups_share_one_first_funder_definition(journey):
    from src.kb.onchain_clustering import _Ledger

    ledger = _Ledger(journey, fb.NAMESPACE, "eip155:1")
    funders = ledger.first_funders()
    assert funders[fb.lower("exampla-treasury")] == [fb.lower("exampla-deployer")]
    origin_hop = __import__(
        "src.kb.onchain", fromlist=["contract_origin"]
    ).contract_origin(
        journey, fb.NAMESPACE, "eip155:1", fb.ETH["exampla-token-contract"]
    )["deployer_funding_chain"][0]
    assert origin_hop["first_inbound"][0]["from"] == fb.lower("exampla-treasury")


def test_the_committed_calibration_report_equals_a_rerun():
    assert cal.REPORT.read_text() == cal.render_report(cal.calibrate())
    served = cal.served_calibration()
    assert served["recommended_min_score"] == cal.DEFAULT_MIN_SCORE
    assert served["measured_at_default"]["fpr"] <= cal.TARGET_FPR
