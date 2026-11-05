"""Blockscout as the declared fallback of the Etherscan source (FA22, #2861).

Offline only: the Blockscout answers are the pinned Etherscan fixture ledger re-keyed to the
Blockscout host and action names (the same documented ``status/message/result`` envelopes).
Live behaviour of eth.blockscout.com is not verified here (``LIVE_VERIFICATION``).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl

import duckdb
import pytest

from src.ingestion import onchain as acq
from src.ingestion.quota import QuotaLedger
from src.ingestion.source_packs import validate_source_pack
from src.kb import onchain
from src.osint.provenance import trace_artifact
from tests.unit.onchain import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
KEY = "fixture-secret-not-a-real-key"
BLOCKSCOUT_API = "eth.blockscout.com/api"


@pytest.fixture()
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


@pytest.fixture(autouse=True)
def _no_key_no_switch(monkeypatch):
    monkeypatch.delenv("NOESIS_ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv(acq.FALLBACK_SWITCH, raising=False)


def blockscout_responses() -> dict[str, dict]:
    """The Etherscan fixture ledger as Blockscout would be asked for it (no chainid, own action names)."""
    out = {}
    for key, entry in fb.etherscan_responses()["responses"].items():
        path, _, query = key.partition("?")
        assert path == fb.API
        params = dict(parse_qsl(query))
        wanted = (params["module"], params["action"])
        if wanted not in acq.PROVIDER_ACTIONS[acq.BLOCKSCOUT]:
            continue  # not served by the fallback: never requested, so never answered
        params.pop("chainid")
        params["module"], params["action"] = acq.PROVIDER_ACTIONS[acq.BLOCKSCOUT][
            wanted
        ]
        out[fb._key(BLOCKSCOUT_API, **params)] = entry
    return out


def spy_transport(seen: list):
    transport = acq.fixture_transport(blockscout_responses())

    def spy(**kwargs):
        seen.append(kwargs)
        return transport(**kwargs)

    return spy


def kwargs(request_id: str, transport, **extra):
    value = {
        "request_id": request_id,
        "principal_id": "tester",
        "scopes": fb.SCOPES,
        "transport": transport,
        "now": lambda: fb.NOW_MS,
    }
    value.update(extra)
    return value


def ledger(tmp_path, limits, clock=lambda: 1_790_000_000.0):
    return QuotaLedger(
        tmp_path / "quota.sqlite",
        config={"hosts": {"api.etherscan.io": {"limits": limits}}},
        clock=clock,
        sleep=lambda s: None,
    )


def test_blockscout_is_the_declared_keyless_fallback_and_live_is_unverified():
    manifest = json.loads((ROOT / "config/source_packs/onchain.json").read_text())
    validate_source_pack(manifest)
    assert BLOCKSCOUT_API not in json.dumps(
        manifest
    )  # pack (and its contract) unchanged
    etherscan = acq.load_source(acq.ETHERSCAN)
    fallback = acq.fallback_source(etherscan)
    assert fallback["source_id"] == acq.BLOCKSCOUT
    assert fallback["endpoint"] == "https://" + BLOCKSCOUT_API
    assert (
        fallback["auth"] == {"kind": "none"}
        and fallback["fallback_for"] == acq.ETHERSCAN
    )
    assert set(acq.FALLBACKS[acq.ETHERSCAN]["when"]) == {
        "credential_missing",
        "budget_exhausted",
    }
    assert sorted(fallback["operations"]) == ["address-funding", "address-transactions"]
    assert fallback["budgets"] == etherscan["budgets"]
    assert fallback["policy"]["attribution"].startswith("Data from Blockscout")
    assert fallback["policy"]["read_only"] is True
    assert "fixture" not in fallback and "fallback" not in fallback
    assert acq.FALLBACKS[acq.ETHERSCAN]["live_status"] == "unverified-live"
    assert acq.LIVE_VERIFICATION[acq.BLOCKSCOUT]["status"] == "unverified-live"
    assert acq.PROVIDER_CONTRACTS[acq.BLOCKSCOUT]["status"] == "fallback"
    assert acq.fallback_source(acq.load_source(acq.ESPLORA)) is None


def test_without_a_key_blockscout_answers_and_receipts_and_records_name_it(conn):
    seen: list = []
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs("fb-1", spy_transport(seen), api_key=None),
    )
    assert receipt["status"] == "acquired" and receipt["added"] > 0
    assert receipt["source_id"] == acq.ETHERSCAN
    assert receipt["provider"] == acq.BLOCKSCOUT
    assert receipt["fallback"] == {
        "from": acq.ETHERSCAN,
        "to": acq.BLOCKSCOUT,
        "reason": "credential_missing",
        "detail": "NOESIS_ETHERSCAN_API_KEY not configured",
    }
    assert receipt["credential"] == "not_required"
    assert receipt["endpoint"] == "https://" + BLOCKSCOUT_API
    assert receipt["license"]["id"] == "blockscout-hosted-terms"
    # Only Blockscout was asked, in its own action names, keyless and without chainid.
    assert {kw["url"] for kw in seen} == {"https://" + BLOCKSCOUT_API}
    assert all(
        "apikey" not in kw["params"] and "chainid" not in kw["params"] for kw in seen
    )
    assert ("block", "eth_block_number") in {
        (kw["params"]["module"], kw["params"]["action"]) for kw in seen
    }

    record = json.loads(
        conn.execute(
            "SELECT record_json FROM onchain_observations WHERE observation_id=?",
            [receipt["observation_id"]],
        ).fetchone()[0]
    )
    assert record["provider"] == acq.BLOCKSCOUT
    assert record["requested_source"] == acq.ETHERSCAN
    assert record["fallback"]["reason"] == "credential_missing"
    assert record["publisher"] == "Blockscout"
    assert record["attribution"] == "Data from Blockscout (eth.blockscout.com)"
    assert {r["host"] for r in record["locator"]["requests"]} == {"eth.blockscout.com"}

    # Ledger facts are those the Etherscan journey yields for the same fictional ledger.
    etherscan = acq.acquire_address_transactions(
        conn,
        "onchain-etherscan",
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs(
            "es-1",
            acq.fixture_transport(fb.etherscan_responses()["responses"]),
            api_key=KEY,
        ),
    )
    assert etherscan["provider"] == acq.ETHERSCAN and etherscan["fallback"] is None
    es_record = json.loads(
        conn.execute(
            "SELECT record_json FROM onchain_observations WHERE observation_id=?",
            [etherscan["observation_id"]],
        ).fetchone()[0]
    )
    assert record["facts"] == es_record["facts"]

    # Citations and provenance name the provider that answered.
    trace = trace_artifact(conn, document_id=receipt["observation_id"])
    assert trace["cited"]
    assert trace["chain"][0]["source_pack_source"] == acq.BLOCKSCOUT
    assert trace["chain"][1]["receipt"]["fallback"]["reason"] == "credential_missing"
    observed = onchain.address_observations(
        conn, fb.NAMESPACE, "eip155:1", fb.ETH["journey-user"]
    )
    assert observed["status"] != "unknown"
    assert acq.BLOCKSCOUT in json.dumps(observed)


def test_funding_runs_through_the_fallback(conn):
    receipt = acq.acquire_address_funding(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["exampla-treasury"],
        **kwargs("fund", acq.fixture_transport(blockscout_responses())),
    )
    assert receipt["status"] == "acquired" and receipt["provider"] == acq.BLOCKSCOUT
    assert receipt["requests"] == 2


def test_operations_blockscout_does_not_serve_are_unavailable_not_empty(conn):
    sent: list = []
    origin = acq.acquire_contract_origin(
        conn,
        fb.NAMESPACE,
        fb.ETH["exampla-token-contract"],
        **kwargs("origin", lambda **kw: sent.append(kw)),
    )
    tx = acq.acquire_transaction(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.TX["deploy"],
        **kwargs("tx", lambda **kw: sent.append(kw)),
    )
    for result, operation in ((origin, "contract-origin"), (tx, "transaction")):
        assert result["status"] == "unavailable"
        assert result["failure_type"] == "unsupported_by_provider"
        assert result["provider"] == acq.BLOCKSCOUT and result["operation"] == operation
        assert result["fallback"]["reason"] == "credential_missing"
        assert "observation_id" not in result and "added" not in result
    assert not sent
    assert not onchain.table_exists(conn, "onchain_acquisitions")
    assert (
        onchain.contract_origin(
            conn, fb.NAMESPACE, "eip155:1", fb.ETH["exampla-token-contract"]
        )["status"]
        != "acquired"
    )


def test_an_unsupported_action_is_never_sent_and_mid_run_is_an_unavailable_receipt(
    conn, monkeypatch
):
    fallback = acq.fallback_source(acq.load_source(acq.ETHERSCAN))
    sent: list = []
    session = acq.Session(
        fallback,
        acq.AcquisitionBudget(
            allowlist=("eth.blockscout.com",),
            max_requests=5,
            max_bytes=1000,
            timeout_s=1,
        ),
        lambda **kw: sent.append(kw),
        None,
    )
    for module, action in (
        ("contract", "getcontractcreation"),
        ("proxy", "eth_getTransactionByHash"),
    ):
        with pytest.raises(acq.AcquisitionError) as exc:
            acq.explorer_get(
                session,
                fallback["endpoint"],
                {"chainid": 1, "module": module, "action": action},
            )
        assert exc.value.code == "unsupported_by_provider"
    assert not sent and session.budget.used == 0

    # If the declared mapping loses an action the run needs, the receipt says unavailable.
    actions = dict(acq.PROVIDER_ACTIONS[acq.BLOCKSCOUT])
    actions.pop(("account", "tokentx"))
    monkeypatch.setitem(acq.PROVIDER_ACTIONS, acq.BLOCKSCOUT, actions)
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs("partial", acq.fixture_transport(blockscout_responses())),
    )
    assert receipt["status"] == "unavailable"
    assert receipt["failure_type"] == "unsupported_by_provider"
    assert receipt["provider"] == acq.BLOCKSCOUT and "observation_id" not in receipt
    assert (
        onchain.address_observations(
            conn, fb.NAMESPACE, "eip155:1", fb.ETH["journey-user"]
        )["status"]
        == "unknown"
    )


def test_an_exhausted_etherscan_budget_routes_to_blockscout(
    conn, monkeypatch, tmp_path
):
    quota = ledger(tmp_path, [{"window": "day", "requests": 20}])
    quota.acquire("https://api.etherscan.io/v2/api", requests=15)
    monkeypatch.setattr(acq, "_quota_ledger", lambda: quota)
    seen: list = []
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs("budget", spy_transport(seen), api_key=KEY),
    )
    assert receipt["status"] == "acquired" and receipt["provider"] == acq.BLOCKSCOUT
    assert receipt["fallback"]["reason"] == "budget_exhausted"
    assert "20 requests per day" in receipt["fallback"]["detail"]
    assert receipt["credential"] == "not_required"
    # The Etherscan key never leaves for another provider.
    assert all(KEY not in json.dumps(kw) for kw in seen)
    assert KEY not in json.dumps(receipt)


def test_budget_exhaustion_needs_a_window_that_does_not_reopen_soon(
    monkeypatch, tmp_path
):
    endpoint = "https://api.etherscan.io/v2/api"
    per_second = ledger(tmp_path, [{"window": "second", "requests": 3}])
    per_second.acquire(endpoint, requests=3)
    monkeypatch.setattr(acq, "_quota_ledger", lambda: per_second)
    assert acq.budget_exhausted(endpoint, 11) is None  # waited out by the transport

    daily = ledger(tmp_path / "d", [{"window": "day", "requests": 100}])
    monkeypatch.setattr(acq, "_quota_ledger", lambda: daily)
    assert acq.budget_exhausted(endpoint, 11) is None
    daily.acquire(endpoint, requests=95)
    assert "100 requests per day" in acq.budget_exhausted(endpoint, 11)

    blocked = ledger(tmp_path / "b", [])
    blocked.block(endpoint, 3600, "HTTP 429")
    monkeypatch.setattr(acq, "_quota_ledger", lambda: blocked)
    assert "retry later" in acq.budget_exhausted(endpoint, 1)

    monkeypatch.setattr(acq, "_quota_ledger", lambda: None)  # quotas disabled
    assert acq.budget_exhausted(endpoint, 10**9) is None


def test_with_a_key_and_budget_left_etherscan_answers(conn, monkeypatch, tmp_path):
    quota = ledger(tmp_path, [{"window": "day", "requests": 100_000}])
    monkeypatch.setattr(acq, "_quota_ledger", lambda: quota)
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs(
            "keyed",
            acq.fixture_transport(fb.etherscan_responses()["responses"]),
            api_key=KEY,
        ),
    )
    assert receipt["provider"] == acq.ETHERSCAN and receipt["fallback"] is None
    assert receipt["credential"] == "configured"


def test_the_fallback_can_be_switched_off(conn, monkeypatch, tmp_path):
    monkeypatch.setenv(acq.FALLBACK_SWITCH, "off")
    sent: list = []
    result = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs("off", lambda **kw: sent.append(kw)),
    )
    assert result["status"] == "no_provider_configured" and not sent
    # With a key and an exhausted budget, Etherscan is still the only provider asked.
    quota = ledger(tmp_path, [{"window": "day", "requests": 1}])
    quota.acquire("https://api.etherscan.io/v2/api")
    monkeypatch.setattr(acq, "_quota_ledger", lambda: quota)
    seen: list = []
    transport = acq.fixture_transport(fb.etherscan_responses()["responses"])
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs(
            "off-keyed",
            lambda **kw: seen.append(kw) or transport(**kw),
            api_key=KEY,
        ),
    )
    assert receipt["provider"] == acq.ETHERSCAN and receipt["fallback"] is None
    assert {kw["url"] for kw in seen} == {"https://api.etherscan.io/v2/api"}


def test_fallback_receipts_reach_the_investigation_audit_detail(conn):
    from src.osint.investigations import investigation_audit
    from src.provisioning import store

    store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO provisioned_kgs (name, description, status, created_at, updated_at) VALUES "
        "('chain-case','t','deployed',now(),now())"
    )
    acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        fb.ETH["journey-user"],
        **kwargs(
            "audit",
            acq.fixture_transport(blockscout_responses()),
            investigation="chain-case",
        ),
    )
    trail = investigation_audit(conn, "chain-case")["audit_trail"]
    events = [e for e in trail if e["event"] == "onchain-observation"]
    assert events and acq.BLOCKSCOUT in json.dumps(events, default=str)
    assert "credential_missing" in json.dumps(events, default=str)
