"""Bounded, receipted acquisition of explorer observations (#2054, #2056)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion import onchain as acq
from src.ingestion.source_packs import SourcePackConformance, validate_source_pack
from src.kb import onchain
from src.osint.provenance import trace_artifact
from tests.unit.onchain import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
KEY = "fixture-secret-not-a-real-key"


@pytest.fixture()
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


def eth_transport():
    return acq.fixture_transport(fb.etherscan_responses()["responses"])


def call(conn, request_id="r1", **extra):
    kwargs = {
        "request_id": request_id,
        "principal_id": "tester",
        "scopes": fb.SCOPES,
        "transport": eth_transport(),
        "api_key": KEY,
        "now": lambda: fb.NOW_MS,
    }
    kwargs.update(extra)
    return acq.acquire_address_transactions(
        conn, fb.NAMESPACE, "eip155:1", fb.ETH["journey-user"], **kwargs
    )


def test_fixtures_are_pinned_and_in_sync():
    for path, text in fb.build().items():
        assert Path(path).read_text() == text, (
            f"{path} drifted; run python -m tests.unit.onchain.fixture_builder"
        )


def test_source_pack_validates_and_replays_offline():
    manifest = json.loads((ROOT / "config/source_packs/onchain.json").read_text())
    pack = validate_source_pack(manifest)
    assert pack["domains"] == ["onchain"]
    assert {s["source_id"] for s in pack["sources"]} == set(fb.RAW_FILES)
    report = SourcePackConformance(ROOT).offline(manifest)
    assert report["valid"] and report["coverage"]["verified"] == 3
    etherscan = next(s for s in pack["sources"] if s["source_id"] == acq.ETHERSCAN)
    assert etherscan["auth"] == {
        "kind": "required-secret",
        "secret_ref": "NOESIS_ETHERSCAN_API_KEY",
    }


def test_provider_audit_covers_two_explorers_and_two_label_datasets_and_live_is_unverified():
    explorers = [
        p for p, c in acq.PROVIDER_CONTRACTS.items() if c["kind"] == "explorer"
    ]
    labels = [
        p for p, c in acq.PROVIDER_CONTRACTS.items() if c["kind"] == "label-dataset"
    ]
    assert len(explorers) >= 2 and len(labels) >= 2
    selected = {
        p for p, c in acq.PROVIDER_CONTRACTS.items() if c["status"] == "selected"
    }
    assert selected == set(acq.LIVE_VERIFICATION) == set(fb.RAW_FILES)
    assert all(v["status"] == "unverified-live" for v in acq.LIVE_VERIFICATION.values())
    doc = (ROOT / "docs/security/onchain-source-access.md").read_text()
    for provider in acq.PROVIDER_CONTRACTS:
        assert provider in doc


def test_without_a_key_ethereum_is_inert_and_writes_nothing(conn, monkeypatch):
    monkeypatch.delenv("NOESIS_ETHERSCAN_API_KEY", raising=False)
    sent = []
    result = call(conn, api_key=None, transport=lambda **kw: sent.append(kw))
    assert result["status"] == "no_provider_configured" and not sent
    assert not onchain.table_exists(conn, "onchain_acquisitions")


def test_the_key_goes_only_in_the_documented_parameter_and_never_into_storage(conn):
    seen = []
    transport = eth_transport()

    def spy(**kwargs):
        seen.append(kwargs)
        return transport(**kwargs)

    receipt = call(conn, transport=spy)
    assert receipt["status"] == "acquired" and receipt["credential"] == "configured"
    assert all(kw["params"].get("apikey") == KEY for kw in seen)
    assert all(KEY not in json.dumps(kw["headers"]) for kw in seen)
    for table in onchain.TABLES:
        if onchain.table_exists(conn, table):
            dump = json.dumps(
                conn.execute(f"SELECT * FROM {table}").fetchall(), default=str
            )
            assert KEY not in dump, table


def test_request_ids_replay_and_refuse_reuse(conn):
    first = call(conn)
    assert call(conn) == first  # replay: the stored receipt, no new request
    with pytest.raises(acq.AcquisitionError) as exc:
        call(conn, from_block=5)
    assert exc.value.code == "request_id_reused"


def test_reacquiring_unchanged_data_adds_nothing(conn):
    first = call(conn, request_id="a")
    again = call(conn, request_id="b", now=lambda: fb.NOW_MS + 1000)
    assert first["added"] > 0 and again["status"] == "acquired" and again["added"] == 0


def test_budget_allowlist_and_redirect_policy(conn):
    def redirect(**kwargs):
        return {
            "status": 200,
            "content": b"{}",
            "final_url": "https://evil.example/api",
        }

    receipt = call(conn, request_id="redir", transport=redirect)
    assert receipt["status"] == "failed" and receipt["failure_type"] == "network_policy"
    source = acq.load_source(acq.ETHERSCAN)
    budget = acq.AcquisitionBudget(
        allowlist=("api.etherscan.io",), max_requests=1, max_bytes=10, timeout_s=1
    )
    session = acq.Session(
        source, budget, lambda **kw: {"status": 200, "content": b"1"}, KEY
    )
    with pytest.raises(acq.AcquisitionError) as exc:
        session.get("https://other.example/api")
    assert exc.value.code == "host_not_allowlisted"
    session.get("https://api.etherscan.io/v2/api")
    with pytest.raises(acq.AcquisitionError) as exc:
        session.get("https://api.etherscan.io/v2/api")
    assert exc.value.code == "budget_exhausted"
    assert "apikey" not in json.dumps(budget.log)


def test_rate_limits_and_bad_keys_are_failed_receipts_without_retry(conn):
    calls = []

    def limited(**kwargs):
        calls.append(kwargs)
        return {
            "status": 200,
            "content": json.dumps(
                {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
            ).encode(),
        }

    receipt = call(conn, request_id="limited", transport=limited)
    assert (
        receipt["status"] == "failed"
        and receipt["failure_type"] == "rate_limited"
        and len(calls) == 1
    )
    bad = call(
        conn,
        request_id="badkey",
        transport=lambda **kw: {
            "status": 200,
            "content": json.dumps(
                {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
            ).encode(),
        },
    )
    assert bad["failure_type"] == "invalid_api_key"
    http = call(
        conn,
        request_id="http429",
        transport=lambda **kw: {"status": 429, "content": b""},
    )
    assert http["failure_type"] == "rate_limited" and http["retries"] == 0


def test_the_real_default_transport_is_used_with_budget_timeout_and_byte_cap(
    conn, monkeypatch
):
    from src.ingestion.source_pack_runtime import (
        HTTPSPageAdapter,
        SourcePackError,
        _validate_redirect,
    )

    seen = []
    transport = eth_transport()

    def fake(**kwargs):
        seen.append(kwargs)
        return transport(**kwargs)

    monkeypatch.setattr(HTTPSPageAdapter, "_request", staticmethod(fake))
    receipt = call(conn, request_id="default", transport=None)
    assert receipt["status"] == "acquired"
    source = acq.load_source(acq.ETHERSCAN)
    assert {kw["timeout"] for kw in seen} == {source["budgets"]["timeout_ms"] / 1000}
    assert {kw["max_bytes"] for kw in seen} == {source["budgets"]["max_bytes"]}
    assert {kw["url"] for kw in seen} == {source["endpoint"]}
    with pytest.raises(
        SourcePackError
    ):  # the default transport refuses cross-host redirects
        _validate_redirect(
            "https://api.etherscan.io/v2/api",
            "https://example.org/x",
            resolver=lambda h: ["8.8.8.8"],
        )


def test_bounded_pages_and_truncated_windows(conn):
    responses = fb.etherscan_responses()["responses"]
    user = fb.lower("journey-user")
    small = dict(responses)
    rows = responses[fb._list("txlist", user)]["body"]["result"]
    small[
        fb._key(
            fb.API,
            action="txlist",
            address=user,
            chainid=1,
            endblock=fb.ETH_HEAD,
            module="account",
            offset=2,
            page=1,
            sort="asc",
            startblock=0,
        )
    ] = {"status": 200, "body": {"status": "1", "message": "OK", "result": rows[:2]}}
    small[
        fb._key(
            fb.API,
            action="tokentx",
            address=user,
            chainid=1,
            endblock=fb.ETH_HEAD,
            module="account",
            offset=2,
            page=1,
            sort="asc",
            startblock=0,
        )
    ] = responses[fb._list("tokentx", user)]
    receipt = acq.acquire_address_transactions(
        conn,
        fb.NAMESPACE,
        "eip155:1",
        user,
        request_id="pages",
        principal_id="t",
        scopes=fb.SCOPES,
        page_size=2,
        max_pages=1,
        transport=acq.fixture_transport(small),
        api_key=KEY,
    )
    assert receipt["status"] == "acquired" and receipt["requests"] == 3
    coverage = onchain.address_observations(conn, fb.NAMESPACE, "eip155:1", user)[
        "coverage"
    ]
    assert [c["complete"] for c in coverage] == [True, False]
    assert coverage[0]["to_block"] == 18_099_999
    with pytest.raises(acq.AcquisitionError):
        call(conn, request_id="toolarge", page_size=5000)


def test_contract_origin_reads_the_creation_transaction_when_the_explorer_omits_the_block(
    conn,
):
    receipts = fb.run_journey(conn, acq.ETHERSCAN)
    assert [r["status"] for r in receipts] == ["acquired"] * 4
    origin = onchain.contract_origin(
        conn, fb.NAMESPACE, "eip155:1", fb.ETH["exampla-token-contract"]
    )
    assert origin["deployment"]["block_number"] == 18_000_100
    assert origin["deployment"]["timestamp"].endswith("Z")
    assert receipts[2]["requests"] <= 7


def test_contract_origin_of_an_account_is_not_a_contract(conn):
    responses = dict(fb.etherscan_responses()["responses"])
    user = fb.lower("journey-user")
    responses[
        fb._key(
            fb.API,
            action="getcontractcreation",
            chainid=1,
            contractaddresses=user,
            module="contract",
        )
    ] = {
        "status": 200,
        "body": {"status": "0", "message": "No data found", "result": None},
    }
    receipt = acq.acquire_contract_origin(
        conn,
        fb.NAMESPACE,
        user,
        request_id="acct",
        principal_id="t",
        scopes=fb.SCOPES,
        transport=acq.fixture_transport(responses),
        api_key=KEY,
    )
    assert receipt["status"] == "not_a_contract"
    assert (
        onchain.contract_origin(conn, fb.NAMESPACE, "eip155:1", user)["status"]
        == "unknown"
    )


def test_no_data_envelopes_with_a_null_result_are_empty_answers():
    for body in (
        {"status": "0", "message": "No data found", "result": None},
        {"status": "0", "message": "No records found", "result": []},
        {"status": "0", "message": "No transactions found", "result": ""},
    ):
        assert acq.etherscan_result(json.dumps(body).encode()) == []
    with pytest.raises(acq.AcquisitionError) as exc:
        acq.etherscan_result(
            json.dumps({"status": "0", "message": "NOTOK", "result": None}).encode()
        )
    assert exc.value.code == "provider_error"


def test_value_sent_with_a_deployment_is_the_contracts_first_funding(conn):
    contract, deployer = (
        fb.lower("exampla-token-contract"),
        fb.lower("exampla-deployer"),
    )
    creation = {
        "tx_hash": fb.TX["deploy"],
        "from": deployer,
        "to": None,
        "value": "7",
        "status": "success",
        "block_number": 18_000_100,
        "contract_created": contract,
    }
    later = {
        "tx_hash": fb.TX["buyer-to-contract"],
        "from": fb.lower("first-buyer"),
        "to": contract,
        "value": "9",
        "status": "success",
        "block_number": 18_000_200,
    }
    first = acq.first_inbound(contract, [later, creation])
    assert [f["tx_hash"] for f in first] == [
        fb.TX["deploy"],
        fb.TX["buyer-to-contract"],
    ]
    assert first[0]["from"] == deployer
    # A creation that sends no value, or creates another contract, is not inbound funding.
    assert acq.first_inbound(contract, [{**creation, "value": "0"}]) == []
    assert acq.first_inbound(deployer, [creation]) == []
    # Through the adapter: the contract's txlist starts with a value-bearing creation.
    responses = dict(fb.etherscan_responses()["responses"])
    key = fb._list("txlist", contract, start=18_000_100)
    rows = responses[key]["body"]["result"]
    created = fb._txrow(
        "deploy", 18_000_100, deployer, None, 3 * 10**17, created=contract
    )
    responses[key] = {
        "status": 200,
        "body": {"status": "1", "message": "OK", "result": [created, *rows]},
    }
    acq.acquire_contract_origin(
        conn,
        fb.NAMESPACE,
        contract,
        request_id="value-deploy",
        principal_id="t",
        scopes=fb.SCOPES,
        transport=acq.fixture_transport(responses),
        api_key=KEY,
    )
    origin = onchain.contract_origin(conn, fb.NAMESPACE, "eip155:1", contract)
    funding = origin["contract_funding"]
    assert funding["status"] == "funded"
    assert funding["first_inbound"][0]["tx_hash"] == fb.TX["deploy"]
    assert funding["first_inbound"][0]["from"] == deployer
    assert any(u["kind"] == "internal_transfers" for u in origin["unknowns"])


def test_esplora_transactions_keep_variant_outputs_and_confirmations(conn):
    fb.run_journey(conn, acq.ESPLORA)
    result = onchain.address_observations(
        conn, fb.NAMESPACE, "bitcoin", fb.BTC["wallet-one"]
    )
    assert result["status"] == "observed" and result["count"] == 2
    history = onchain.record_history(
        conn,
        fb.NAMESPACE,
        "transaction",
        f"bip122:000000000019d6689c085ae165831e93|{fb.BTX['cospend']}",
    )
    content = history["revisions"][0]["content"]
    assert [o["address"] for o in content["outputs"]][
        -1
    ] is None  # OP_RETURN kept, not dropped
    assert content["finality"] == "final"


def test_labels_quote_the_source_drop_person_fields_and_record_absence(conn):
    receipts = fb.run_journey(conn, acq.TOKEN_LABELS)
    assert [r["status"] for r in receipts] == ["acquired", "not_labelled_by_source"]
    contract = fb.lower("exampla-token-contract")
    label = onchain.labels_for(conn, fb.NAMESPACE, "eip155:1", [contract])[contract][0]
    assert label["quote"] == "Exampla Token" and label["licence"] == "MIT"
    stored = json.dumps(
        conn.execute("SELECT record_json FROM onchain_observations").fetchall()
    )
    assert "support@" not in stored and "exampla_token" not in stored
    with pytest.raises(acq.AcquisitionError):
        acq.parse_token_label(
            json.dumps({"address": fb.ETH["journey-user"], "name": "X"}).encode(),
            contract,
        )


def test_trace_artifact_follows_an_onchain_observation(conn):
    receipt = call(conn)
    trace = trace_artifact(conn, document_id=receipt["observation_id"])
    assert [s["stage"] for s in trace["chain"]] == [
        "source",
        "acquisition",
        "observation",
        "projection",
    ]
    assert trace["chain"][1]["receipt"]["request_id"] == "r1"
    assert trace["chain"][3]["record_revisions"]
    assert (
        trace_artifact(conn, document_id="onchain-obs:missing")["code"] == "not_found"
    )


def test_receipts_are_logged_into_an_investigation_audit_trail(conn):
    from src.osint.investigations import investigation_audit
    from src.provisioning import store

    store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO provisioned_kgs (name, description, status, created_at, updated_at) VALUES "
        "('chain-case','t','deployed',now(),now())"
    )
    call(conn, investigation="chain-case")
    trail = investigation_audit(conn, "chain-case")["audit_trail"]
    assert any(e["event"] == "onchain-observation" for e in trail)


def test_there_is_no_write_capability_toward_a_ledger():
    source = (ROOT / "src/ingestion/onchain.py").read_text()
    for forbidden in (
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "personal_",
        "private_key",
        '/tx" , method="POST',
    ):
        assert forbidden not in source
    assert acq.READ_RPC_METHODS == {
        "eth_blockNumber",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getBlockByNumber",
    }
    with pytest.raises(acq.AcquisitionError):
        acq._rpc(None, "https://api.etherscan.io/v2/api", "eth_sendRawTransaction")


def test_a_label_the_source_stops_stating_is_withdrawn_not_quoted(conn):
    fb.run_journey(conn, acq.TOKEN_LABELS)
    contract = fb.lower("exampla-token-contract")
    assert onchain.labels_for(conn, fb.NAMESPACE, "eip155:1", [contract])
    gone = acq.acquire_label(
        conn,
        fb.NAMESPACE,
        contract,
        request_id="label-gone",
        principal_id="t",
        scopes=fb.SCOPES,
        transport=acq.fixture_transport({}),
    )
    assert gone["status"] == "not_labelled_by_source"
    assert [c["change"] for c in gone["changes"]] == ["withdrawn"]
    assert onchain.labels_for(conn, fb.NAMESPACE, "eip155:1", [contract]) == {}


def test_reobserving_the_same_coverage_window_adds_no_row_and_keeps_the_generation(
    conn,
):
    fb.run_journey(conn, acq.TOKEN_LABELS)
    before = onchain.generation(conn, fb.NAMESPACE)
    rows = conn.execute("SELECT COUNT(*) FROM onchain_coverage").fetchone()[0]
    again = acq.acquire_label(
        conn,
        fb.NAMESPACE,
        fb.ETH["exampla-token-contract"],
        request_id="label-again",
        principal_id="t",
        scopes=fb.SCOPES,
        transport=acq.fixture_transport(fb.label_responses()["responses"]),
    )
    assert again["added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM onchain_coverage").fetchone()[0] == rows
    assert onchain.generation(conn, fb.NAMESPACE) == before


def test_observation_ids_are_distinct_per_namespace(conn):
    first = call(conn, request_id="ns-a")
    second = acq.acquire_address_transactions(
        conn,
        "other-namespace",
        "eip155:1",
        fb.ETH["journey-user"],
        request_id="ns-b",
        principal_id="t",
        scopes=fb.SCOPES,
        transport=eth_transport(),
        api_key=KEY,
        now=lambda: fb.NOW_MS,
    )
    assert first["observation_id"] != second["observation_id"] and second["added"] > 0
