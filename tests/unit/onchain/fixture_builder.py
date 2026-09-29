"""Build the pinned On-chain Observations fixtures from one fictional ledger.

Every address and hash below is fictional and correctly shaped: EVM addresses
are the last 20 bytes of Keccak-256 of a label (EIP-55 checksummed), Bitcoin
addresses are P2WPKH programs of SHA-256 of a label (valid bech32 checksum),
and transaction hashes are SHA-256 of a label. None is tied to a real person.

The native responses follow the providers' documented shapes (Etherscan V2
``status/message/result`` and proxy JSON-RPC envelopes, Esplora transaction
objects, ethereum-lists token files). ``python -m tests.unit.onchain.fixture_builder``
rewrites ``tests/fixtures/onchain/*.json`` and the source-pack fixtures, and pins
their hashes into ``config/source_packs/onchain.json``; a test fails when they drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

from src.kb.onchain_identifiers import (
    BITCOIN,
    ETHEREUM,
    fixture_btc_address,
    fixture_evm_address,
    fixture_tx_hash,
)

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "tests/fixtures/onchain"
OUT = ROOT / "tests/fixtures/source_packs"
PACK = ROOT / "config/source_packs/onchain.json"
NOTE = (
    "Authored offline fixture in the provider's documented response shape; every address and hash is fictional "
    "(derived from a label) and values are invented. Not live evidence."
)
ETH_HEAD = 18_200_000
BTC_HEAD = 812_000
NOW_MS = 1_790_000_000_000
NAMESPACE = "onchain-fixture"
SCOPES = {
    "knowledge:onchain:write",
    "knowledge:onchain:read",
    "knowledge:onchain:review",
}
ETH = {
    name: fixture_evm_address(name)
    for name in (
        "exampla-token-contract",
        "exampla-deployer",
        "exampla-treasury",
        "upstream-funder",
        "first-buyer",
        "journey-user",
        "journey-user-second",
        "journey-deposit",
        "journey-exchange",
        "journey-sender",
    )
}
BTC = {
    name: fixture_btc_address(name)
    for name in ("wallet-one", "wallet-two", "wallet-change", "merchant", "faucet")
}
TX = {
    name: fixture_tx_hash(name)
    for name in (
        "upstream-to-treasury",
        "treasury-to-deployer",
        "deploy",
        "buyer-to-contract",
        "sender-to-user",
        "user-to-deposit",
        "user-failed",
        "token-to-user",
        "second-to-deposit",
        "deposit-sweep",
    )
}
BTX = {
    name: fixture_tx_hash(name, evm=False)
    for name in ("faucet-to-wallet", "cospend", "prev-one", "prev-two")
}


def lower(label: str) -> str:
    return ETH[label].lower()


def _ts(block: int) -> int:
    return 1_695_000_000 + (block - 18_000_000) * 12


def _block_hash(block: int, variant: str = "") -> str:
    return (
        "0x"
        + hashlib.sha256(f"noesis-fixture-block:{block}{variant}".encode()).hexdigest()
    )


def _txrow(
    name: str,
    block: int,
    frm: str | None,
    to: str | None,
    value: int,
    *,
    failed: bool = False,
    created: str | None = None,
) -> dict[str, str]:
    return {
        "blockNumber": str(block),
        "timeStamp": str(_ts(block)),
        "hash": TX[name],
        "nonce": "1",
        "blockHash": _block_hash(block),
        "transactionIndex": "3",
        "from": frm or "",
        "to": to or "",
        "value": str(value),
        "gas": "21000",
        "gasPrice": "20000000000",
        "isError": "1" if failed else "0",
        "txreceipt_status": "0" if failed else "1",
        "input": "0x",
        "contractAddress": created or "",
        "cumulativeGasUsed": "210000",
        "gasUsed": "21000",
        "confirmations": str(ETH_HEAD - block + 1),
        "methodId": "0x",
        "functionName": "",
    }


def _ok(result: Any) -> dict[str, Any]:
    return {"status": 200, "body": {"status": "1", "message": "OK", "result": result}}


def _none() -> dict[str, Any]:
    return {
        "status": 200,
        "body": {"status": "0", "message": "No transactions found", "result": []},
    }


def _rpc(result: Any) -> dict[str, Any]:
    return {"status": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": result}}


def _key(path: str, **params: Any) -> str:
    return (
        path
        + "?"
        + "&".join(
            f"{k}={v}" for k, v in sorted((str(k), str(v)) for k, v in params.items())
        )
    )


API = "api.etherscan.io/v2/api"


def _list(action: str, address: str, start: int = 0, size: int = 100) -> str:
    return _key(
        API,
        action=action,
        address=address,
        chainid=1,
        endblock=ETH_HEAD,
        module="account",
        offset=size,
        page=1,
        sort="asc",
        startblock=start,
    )


def etherscan_responses() -> dict[str, Any]:
    user, deposit, second = (
        lower("journey-user"),
        lower("journey-deposit"),
        lower("journey-user-second"),
    )
    contract, deployer = lower("exampla-token-contract"), lower("exampla-deployer")
    treasury, upstream = lower("exampla-treasury"), lower("upstream-funder")
    deploy_block = 18_000_100
    responses = {
        _key(API, action="eth_blockNumber", chainid=1, module="proxy"): _rpc(
            hex(ETH_HEAD)
        ),
        _list("txlist", user): _ok(
            [
                _txrow(
                    "sender-to-user",
                    18_050_000,
                    lower("journey-sender"),
                    user,
                    2 * 10**18,
                ),
                _txrow("user-to-deposit", 18_100_000, user, deposit, 10**18),
                _txrow(
                    "user-failed",
                    18_100_500,
                    user,
                    lower("first-buyer"),
                    1,
                    failed=True,
                ),
            ]
        ),
        _list("tokentx", user): _ok(
            [
                {
                    "blockNumber": "18120000",
                    "timeStamp": str(_ts(18_120_000)),
                    "hash": TX["token-to-user"],
                    "nonce": "4",
                    "blockHash": _block_hash(18_120_000),
                    "from": deployer,
                    "contractAddress": contract,
                    "to": user,
                    "value": "1000000000000000000000",
                    "tokenName": "Exampla Token",
                    "tokenSymbol": "EXA",
                    "tokenDecimal": "18",
                    "transactionIndex": "9",
                    "gas": "60000",
                    "gasPrice": "20000000000",
                    "gasUsed": "52000",
                    "cumulativeGasUsed": "900000",
                    "input": "deprecated",
                    "logIndex": "7",
                    "confirmations": str(ETH_HEAD - 18_120_000 + 1),
                }
            ]
        ),
        _list("txlist", deposit): _ok(
            [
                _txrow("user-to-deposit", 18_100_000, user, deposit, 10**18),
                _txrow("second-to-deposit", 18_100_100, second, deposit, 9 * 10**17),
                _txrow(
                    "deposit-sweep",
                    18_100_200,
                    deposit,
                    lower("journey-exchange"),
                    19 * 10**17 - 42000 * 10**9,
                ),
            ]
        ),
        _list("tokentx", deposit): _none(),
        # Variant shape: an older explorer answer without blockNumber/timestamp, so the creation
        # transaction is read through the proxy.
        _key(
            API,
            action="getcontractcreation",
            chainid=1,
            contractaddresses=contract,
            module="contract",
        ): _ok(
            [
                {
                    "contractAddress": contract,
                    "contractCreator": deployer,
                    "txHash": TX["deploy"],
                }
            ]
        ),
        _key(
            API,
            action="eth_getTransactionByHash",
            chainid=1,
            module="proxy",
            txhash=TX["deploy"],
        ): _rpc(
            {
                "blockHash": _block_hash(deploy_block),
                "blockNumber": hex(deploy_block),
                "from": deployer,
                "gas": "0x2dc6c0",
                "gasPrice": "0x4a817c800",
                "hash": TX["deploy"],
                "input": "0x6080",
                "nonce": "0x0",
                "to": None,
                "transactionIndex": "0x1",
                "value": "0x0",
                "type": "0x2",
                "chainId": "0x1",
            }
        ),
        _key(
            API,
            action="eth_getTransactionReceipt",
            chainid=1,
            module="proxy",
            txhash=TX["deploy"],
        ): _rpc(
            {
                "status": "0x1",
                "contractAddress": contract,
                "blockNumber": hex(deploy_block),
                "transactionHash": TX["deploy"],
                "gasUsed": "0x1e8480",
                "logs": [],
            }
        ),
        _key(
            API,
            action="eth_getBlockByNumber",
            boolean="false",
            chainid=1,
            module="proxy",
            tag=hex(deploy_block),
        ): _rpc(
            {
                "number": hex(deploy_block),
                "hash": _block_hash(deploy_block),
                "timestamp": hex(_ts(deploy_block)),
                "transactions": [],
            }
        ),
        _list("txlist", contract, start=deploy_block): _ok(
            [
                _txrow(
                    "buyer-to-contract",
                    18_000_200,
                    lower("first-buyer"),
                    contract,
                    10**18,
                ),
            ]
        ),
        _list("txlist", deployer): _ok(
            [
                _txrow(
                    "treasury-to-deployer", 18_000_050, treasury, deployer, 5 * 10**17
                ),
                _txrow("deploy", deploy_block, deployer, None, 0, created=contract),
            ]
        ),
        _list("txlist", treasury): _ok(
            [
                _txrow(
                    "upstream-to-treasury", 17_900_000, upstream, treasury, 3 * 10**18
                ),
                _txrow(
                    "treasury-to-deployer", 18_000_050, treasury, deployer, 5 * 10**17
                ),
            ]
        ),
    }
    return {
        "description": NOTE,
        "provider": "etherscan-v2-mainnet",
        "head_block": ETH_HEAD,
        "responses": dict(sorted(responses.items())),
    }


def _spk(address_label: str) -> str:
    return (
        "0014"
        + hashlib.sha256(("noesis-fixture-btc:" + address_label).encode())
        .digest()[:20]
        .hex()
    )


def _vin(txid: str, vout: int, label: str, value: int) -> dict[str, Any]:
    return {
        "txid": txid,
        "vout": vout,
        "prevout": {
            "scriptpubkey": _spk(label),
            "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": BTC[label],
            "value": value,
        },
        "scriptsig": "",
        "scriptsig_asm": "",
        "witness": ["30440220", "02aa"],
        "is_coinbase": False,
        "sequence": 4294967293,
    }


def _vout(label: str, value: int) -> dict[str, Any]:
    return {
        "scriptpubkey": _spk(label),
        "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
        "scriptpubkey_type": "v0_p2wpkh",
        "scriptpubkey_address": BTC[label],
        "value": value,
    }


def _btx(name: str, height: int, vin: list, vout: list) -> dict[str, Any]:
    return {
        "txid": BTX[name],
        "version": 2,
        "locktime": 0,
        "vin": vin,
        "vout": vout,
        "size": 222,
        "weight": 561,
        "fee": 1410,
        "status": {
            "confirmed": True,
            "block_height": height,
            "block_hash": _block_hash(height, "btc")[2:],
            "block_time": 1_690_000_000 + height,
        },
    }


def esplora_responses() -> dict[str, Any]:
    funding = _btx(
        "faucet-to-wallet",
        811_000,
        [_vin(BTX["prev-one"], 0, "faucet", 60_000)],
        [_vout("wallet-one", 50_000), _vout("faucet", 8_590)],
    )
    cospend = _btx(
        "cospend",
        811_900,
        [
            _vin(BTX["faucet-to-wallet"], 0, "wallet-one", 50_000),
            _vin(BTX["prev-two"], 1, "wallet-two", 20_000),
        ],
        [
            _vout("merchant", 55_000),
            _vout("wallet-change", 13_590),
            # Variant shape: an OP_RETURN output has no scriptpubkey_address; kept with address None.
            {
                "scriptpubkey": "6a0b6e6f657369732d74657374",
                "scriptpubkey_asm": "OP_RETURN",
                "scriptpubkey_type": "op_return",
                "value": 0,
            },
        ],
    )
    host = "blockstream.info/api"
    responses = {
        f"{host}/blocks/tip/height": {"status": 200, "body": str(BTC_HEAD)},
        f"{host}/address/{BTC['wallet-one']}/txs/chain": {
            "status": 200,
            "body": [cospend, funding],
        },
        f"{host}/address/{BTC['wallet-two']}/txs/chain": {
            "status": 200,
            "body": [cospend],
        },
        f"{host}/tx/{BTX['cospend']}": {"status": 200, "body": cospend},
    }
    return {
        "description": NOTE,
        "provider": "blockstream-esplora-bitcoin",
        "head_block": BTC_HEAD,
        "responses": dict(sorted(responses.items())),
    }


def label_responses() -> dict[str, Any]:
    contract = ETH["exampla-token-contract"]
    body = {
        "symbol": "EXA",
        "name": "Exampla Token",
        "type": "ERC20",
        "address": contract,
        "ens_address": "",
        "decimals": 18,
        "website": "https://exampla-token.example",
        "logo": {"src": "", "width": "", "height": "", "ipfs_hash": ""},
        "support": {"email": "support@exampla-token.example", "url": ""},
        "social": {
            "blog": "",
            "chat": "",
            "facebook": "",
            "forum": "",
            "github": "",
            "gitter": "",
            "instagram": "",
            "linkedin": "",
            "reddit": "",
            "slack": "",
            "telegram": "",
            "twitter": "exampla_token",
            "youtube": "",
        },
    }
    path = (
        "raw.githubusercontent.com/MyEtherWallet/ethereum-lists/master/src/tokens/eth"
    )
    return {
        "description": NOTE,
        "provider": "ethereum-lists-tokens",
        "responses": {f"{path}/{contract}.json": {"status": 200, "body": body}},
    }


# The offline journey: each replay is one acquisition call against the fixture transport.
def replays() -> dict[str, list[dict[str, Any]]]:
    return {
        "etherscan-v2-mainnet": [
            {
                "call": "acquire_address_transactions",
                "chain_id": ETHEREUM,
                "identifier": ETH["journey-user"],
            },
            {
                "call": "acquire_address_transactions",
                "chain_id": ETHEREUM,
                "identifier": ETH["journey-deposit"],
            },
            {
                "call": "acquire_contract_origin",
                "chain_id": ETHEREUM,
                "identifier": ETH["exampla-token-contract"],
            },
            {
                "call": "acquire_address_funding",
                "chain_id": ETHEREUM,
                "identifier": ETH["exampla-treasury"],
            },
        ],
        "blockstream-esplora-bitcoin": [
            {
                "call": "acquire_address_transactions",
                "chain_id": BITCOIN,
                "identifier": BTC["wallet-one"],
            },
            {
                "call": "acquire_address_transactions",
                "chain_id": BITCOIN,
                "identifier": BTC["wallet-two"],
            },
            {
                "call": "acquire_transaction",
                "chain_id": BITCOIN,
                "identifier": BTX["cospend"],
            },
        ],
        "ethereum-lists-tokens": [
            {
                "call": "acquire_label",
                "chain_id": ETHEREUM,
                "identifier": ETH["exampla-token-contract"],
            },
            {
                "call": "acquire_label",
                "chain_id": ETHEREUM,
                "identifier": ETH["journey-user"],
            },
        ],
    }


RAW_FILES = {
    "etherscan-v2-mainnet": "etherscan-journey.json",
    "blockstream-esplora-bitcoin": "esplora-journey.json",
    "ethereum-lists-tokens": "ethereum-lists-journey.json",
}
PACK_FIXTURES = {
    "etherscan-v2-mainnet": "onchain-etherscan.json",
    "blockstream-esplora-bitcoin": "onchain-esplora.json",
    "ethereum-lists-tokens": "onchain-ethereum-lists.json",
}
SCENARIOS = {
    "etherscan-v2-mainnet": [
        "address-range",
        "token-transfer-with-log-index",
        "failed-transaction",
        "no-results-envelope",
        "creation-without-block-variant",
        "funding-chain-hop",
        "deposit-sweep",
    ],
    "blockstream-esplora-bitcoin": [
        "common-input",
        "op-return-output-without-address",
        "exhausted-pagination",
        "transaction-by-txid",
    ],
    "ethereum-lists-tokens": [
        "quoted-label",
        "person-linked-fields-dropped",
        "not-labelled-404",
    ],
}


def raw_documents() -> dict[str, dict[str, Any]]:
    return {
        "etherscan-v2-mainnet": etherscan_responses(),
        "blockstream-esplora-bitcoin": esplora_responses(),
        "ethereum-lists-tokens": label_responses(),
    }


def run_journey(
    conn: Any, source_id: str, *, namespace: str = NAMESPACE
) -> list[dict[str, Any]]:
    """Replay one source's calls offline; returns the receipts."""
    from src.ingestion import onchain

    transport = onchain.fixture_transport(raw_documents()[source_id]["responses"])
    receipts = []
    for n, replay in enumerate(replays()[source_id]):
        call = getattr(onchain, replay["call"])
        kwargs: dict[str, Any] = {
            "request_id": f"fixture:{source_id}:{n}",
            "principal_id": "fixture",
            "scopes": SCOPES,
            "transport": transport,
            "now": lambda n=n: NOW_MS + n,
        }
        if source_id == "etherscan-v2-mainnet":
            kwargs["api_key"] = "fixture-secret-not-a-real-key"
        if replay["call"] in {"acquire_contract_origin", "acquire_label"}:
            receipts.append(
                call(
                    conn,
                    namespace,
                    replay["identifier"],
                    chain_id=replay["chain_id"],
                    **kwargs,
                )
            )
        else:
            receipts.append(
                call(
                    conn, namespace, replay["chain_id"], replay["identifier"], **kwargs
                )
            )
    return receipts


def normalized(source_id: str) -> list[dict[str, Any]]:
    """The normalised observations the journey produces: what the pinned fixture replays."""
    conn = duckdb.connect(":memory:")
    try:
        receipts = run_journey(conn, source_id)
        out = []
        for replay, receipt in zip(replays()[source_id], receipts, strict=True):
            row = conn.execute(
                "SELECT record_json FROM onchain_observations WHERE observation_id=?",
                [receipt.get("observation_id")],
            ).fetchone()
            record = json.loads(row[0]) if row else {}
            out.append(
                {
                    "id": f"{receipt['operation']}:{replay['identifier']}",
                    "operation": receipt["operation"],
                    "chain_id": replay["chain_id"],
                    "subject": receipt["subject"],
                    "status": receipt["status"],
                    "facts": record.get("facts"),
                }
            )
        return out
    finally:
        conn.close()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def build() -> dict[str, str]:
    """Render every fixture file; returns ``{path: text}`` without writing."""
    files: dict[str, str] = {}
    for source_id, raw in raw_documents().items():
        files[str(RAW / RAW_FILES[source_id])] = (
            json.dumps(raw, indent=1, sort_keys=True) + "\n"
        )
        pack_fixture = {
            "note": NOTE,
            "native_responses": [f"tests/fixtures/onchain/{RAW_FILES[source_id]}"],
            "replays": replays()[source_id],
            "normalized": normalized(source_id),
            "scenarios": SCENARIOS[source_id],
        }
        files[str(OUT / PACK_FIXTURES[source_id])] = (
            json.dumps(pack_fixture, sort_keys=True, separators=(",", ":")) + "\n"
        )
    manifest = json.loads(PACK.read_text())
    for source in manifest["sources"]:
        text = files[str(OUT / PACK_FIXTURES[source["source_id"]])]
        source["fixture"]["sha256"] = hashlib.sha256(text.encode()).hexdigest()
        source["fixture"]["expected_output_hash"] = _digest(
            json.loads(text)["normalized"]
        )
    files[str(PACK)] = json.dumps(manifest, indent=2) + "\n"
    return files


def main() -> None:
    for path, text in build().items():
        Path(path).write_text(text)


if __name__ == "__main__":
    main()
