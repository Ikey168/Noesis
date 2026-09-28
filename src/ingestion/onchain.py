"""Bounded, receipted acquisition of public-ledger observations (#2053, B03 #2056).

Consumes the sources declared in ``config/source_packs/onchain.json``:

* ``etherscan-v2-mainnet`` - the Etherscan V2 API (Etherscan-compatible
  ``module``/``action`` interface) for Ethereum mainnet (``chainid=1``). It needs
  an API key (``NOESIS_ETHERSCAN_API_KEY``), sent only as the provider's
  documented ``apikey`` query parameter and never stored in a receipt,
  locator or error. Without a key every Ethereum operation is inert and
  returns ``no_provider_configured``.
* ``blockstream-esplora-bitcoin`` - the Esplora REST API on blockstream.info
  for Bitcoin mainnet (no key).
* ``ethereum-lists-tokens`` - the MIT-licensed MyEtherWallet ``ethereum-lists``
  token files, one file per explicitly named contract address, quoted with
  attribution.

It follows the receipt pattern of :mod:`src.ingestion.wayback`: one explicit
identifier per call (never enumeration, never a name), a caller-chosen
``request_id`` with an input hash (a replay returns the stored receipt; reuse
with other inputs is refused), a per-response byte cap, a timeout, a request
budget and an allowlist of the source's own host (the agent-host budget
model), and no automatic retries. Only parsed facts and a hash of the raw
bytes are stored. Observations project into the records of
:mod:`src.kb.onchain` and are traceable with ``trace_artifact``.

Read-only by construction: there is no wallet, key-management, signing or
transaction-submission code path. The only JSON-RPC methods sent are the
read methods in :data:`READ_RPC_METHODS`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.kb.onchain import (
    CONTRACT,
    WRITE_SCOPE,
    OnchainError,
    OnchainStore,
    authorize,
    canonical,
    digest,
    iso,
)
from src.kb.onchain_identifiers import (
    BITCOIN,
    ETHEREUM,
    IdentifierError,
    checksum_address,
    family,
    normalize_address,
    normalize_chain,
    normalize_tx_hash,
)

SOURCE_PACK_PATH = (
    Path(__file__).resolve().parents[2] / "config/source_packs/onchain.json"
)
ADAPTER_VERSION = "onchain-observations-v1"
ETHERSCAN = "etherscan-v2-mainnet"
ESPLORA = "blockstream-esplora-bitcoin"
TOKEN_LABELS = "ethereum-lists-tokens"
SOURCE_FOR_CHAIN = {ETHEREUM: ETHERSCAN, BITCOIN: ESPLORA}
READ_RPC_METHODS = frozenset(
    {
        "eth_blockNumber",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getBlockByNumber",
    }
)
# The provider's documented credential parameter; the key is added to the request only, never logged.
KEY_PARAMS = {ETHERSCAN: "apikey"}
ESPLORA_PAGE = 25  # Esplora returns 25 confirmed transactions per chain page
FUNDING_INBOUND = 3  # first inbound value transfers kept per funding record

_BLOCKED = "no network access to the provider from the build environment; verify with scripts before relying on it"
_VERIFY = "claim still to be verified against the live terms"

# B01 (#2054): per-provider access audit. docs/security/onchain-source-access.md is the long form.
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    ETHERSCAN: {
        "status": "selected",
        "kind": "explorer",
        "documentation": "https://docs.etherscan.io/etherscan-v2",
        "access": "Etherscan V2 REST: module=account action=txlist|tokentx, module=contract "
        "action=getcontractcreation, module=proxy read-only JSON-RPC (eth_blockNumber, "
        "eth_getTransactionByHash, eth_getTransactionReceipt, eth_getBlockByNumber)",
        "authentication": "API key (required secret NOESIS_ETHERSCAN_API_KEY) as the documented apikey parameter",
        "rate_limits": "free tier documented as 5 calls/second and 100,000 calls/day ("
        + _VERIFY
        + ")",
        "pagination": "page/offset with page*offset <= 10,000; sort=asc; bounded by the source's max_pages",
        "terms": "Etherscan API Terms of Service: attribution ('Powered by Etherscan.io APIs') required; bulk "
        "redistribution of API output restricted (" + _VERIFY + ")",
        "stored": "parsed ledger facts only (hashes, blocks, addresses, values) with explorer citations; raw "
        "responses are hashed, not stored",
        "identifiers": [
            "address",
            "tx hash",
            "contract address",
            "chain id (chainid=1 -> eip155:1)",
        ],
        "coverage": "one explicit address, transaction or contract per call; Ethereum mainnet only",
        "unavailable_fallback": "no key: no_provider_configured and nothing is written; a provider failure is a "
        "failed receipt and earlier observations stay current",
    },
    "blockscout-ethereum": {
        "status": "audited-not-selected",
        "kind": "explorer",
        "documentation": "https://docs.blockscout.com/devs/apis/rpc",
        "access": "Etherscan-compatible RPC API (same module/action shape) on eth.blockscout.com/api",
        "authentication": "optional API key",
        "rate_limits": "documented per-instance limits without a key (" + _VERIFY + ")",
        "terms": "open-source explorer (GPL-3.0 code); hosted-instance terms apply to the service ("
        + _VERIFY
        + ")",
        "decision": "same response format as the selected source, so the Etherscan parsers accept it; kept as a "
        "documented alternative and not declared in the source pack until its hosted terms are verified",
    },
    ESPLORA: {
        "status": "selected",
        "kind": "explorer",
        "documentation": "https://github.com/Blockstream/esplora/blob/master/API.md",
        "access": "Esplora REST: /blocks/tip/height, /address/:address/txs/chain[/:last_seen_txid], /tx/:txid",
        "authentication": "none",
        "rate_limits": "undocumented fair-use limits on the public instance; HTTP 429 is a failed receipt, "
        "never retried (" + _VERIFY + ")",
        "pagination": "25 confirmed transactions per page, newest first, continued by last seen txid",
        "terms": "Blockstream public service terms; ledger facts are public data ("
        + _VERIFY
        + ")",
        "stored": "parsed inputs/outputs, block, confirmations; raw responses hashed, not stored",
        "identifiers": [
            "address (base58 / bech32 / bech32m)",
            "txid",
            "chain id bip122 genesis prefix",
        ],
        "coverage": "one explicit address or txid per call; Bitcoin mainnet only; mempool transactions not acquired",
        "unavailable_fallback": "a provider failure is a failed receipt; earlier observations stay current",
    },
    TOKEN_LABELS: {
        "status": "selected",
        "kind": "label-dataset",
        "documentation": "https://github.com/MyEtherWallet/ethereum-lists",
        "access": "raw file per token contract: src/tokens/eth/<EIP-55 address>.json on raw.githubusercontent.com",
        "authentication": "none",
        "rate_limits": "GitHub raw content fair use (" + _VERIFY + ")",
        "terms": "MIT licence (repository LICENSE); quotation with attribution permitted ("
        + _VERIFY
        + ")",
        "stored": "name, symbol, type, decimals and website as stated; support e-mail and social handles dropped",
        "identifiers": ["contract address (EIP-55 in the file name)"],
        "coverage": "token contracts that chose to list themselves; absence of a file is 'not labelled by this "
        "source', never 'unlabelled'",
        "unavailable_fallback": "HTTP 404 is recorded as not labelled; failures are failed receipts",
    },
    "eth-labels": {
        "status": "audited-rejected",
        "kind": "label-dataset",
        "documentation": "https://github.com/dawsbot/eth-labels",
        "decision": "labels are collected from explorer label pages whose own terms restrict redistribution; the "
        "repository licence cannot grant rights it does not hold (" + _VERIFY + ")",
    },
    "etherscan-label-cloud": {
        "status": "audited-rejected",
        "kind": "label-dataset",
        "documentation": "https://etherscan.io/labelcloud",
        "decision": "web pages, not an API; no licence for bulk quotation; scraping is out of scope",
    },
    "ofac-sdn-digital-currency-addresses": {
        "status": "excluded",
        "kind": "label-dataset",
        "documentation": "https://ofac.treasury.gov/specially-designated-nationals-and-blocked-persons-list-sdn-human-readable-lists",
        "decision": "entries name natural persons and are screening inputs; person attribution and screening "
        "determinations are non-goals of this pack",
    },
}
LIVE_VERIFICATION = {
    ETHERSCAN: {
        "status": "unverified-live",
        "last_run": None,
        "result": "not-sent",
        "failure_code": "credential_missing",
        "cause": "NOESIS_ETHERSCAN_API_KEY not configured; " + _BLOCKED,
    },
    ESPLORA: {
        "status": "unverified-live",
        "last_run": None,
        "result": "not-sent",
        "cause": _BLOCKED,
    },
    TOKEN_LABELS: {
        "status": "unverified-live",
        "last_run": None,
        "result": "not-sent",
        "cause": _BLOCKED,
    },
}


class AcquisitionError(OnchainError):
    pass


def load_source(source_id: str, path: Path = SOURCE_PACK_PATH) -> dict[str, Any]:
    pack = json.loads(Path(path).read_text())
    defaults = pack.get("defaults") or {}
    for source in pack["sources"]:
        if source["source_id"] == source_id:
            merged = {**defaults, **source}
            merged["budgets"] = {
                **(defaults.get("budgets") or {}),
                **(source.get("budgets") or {}),
            }
            merged["auth"] = dict(
                source.get("auth") or defaults.get("auth") or {"kind": "none"}
            )
            return merged
    raise AcquisitionError(
        "undeclared_source",
        f"{source_id!r} is not declared in the on-chain source pack",
    )


@dataclass
class AcquisitionBudget:
    """The agent-host budget for one acquisition: host allowlist, request cap, byte cap, timeout."""

    allowlist: tuple[str, ...]
    max_requests: int
    max_bytes: int
    timeout_s: float
    used: int = 0
    bytes: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)


class Session:
    """One budgeted, key-safe request channel to a single declared source."""

    def __init__(
        self,
        source: Mapping[str, Any],
        budget: AcquisitionBudget,
        transport: Callable[..., Any],
        secret: str | None,
    ) -> None:
        self.source, self.budget, self.transport, self._secret = (
            source,
            budget,
            transport,
            secret,
        )
        self.raw = hashlib.sha256()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        allow_404: bool = False,
    ) -> bytes | None:
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme != "https" or host not in self.budget.allowlist:
            raise AcquisitionError(
                "host_not_allowlisted", "request host is not the declared source host"
            )
        if self.budget.used >= self.budget.max_requests:
            raise AcquisitionError("budget_exhausted", "request budget exhausted")
        self.budget.used += 1
        public = {k: v for k, v in dict(params or {}).items()}
        sent = dict(public)
        param = KEY_PARAMS.get(self.source["source_id"])
        if self._secret and param:
            sent[param] = self._secret
        response = self.transport(
            url=url,
            params=sent,
            headers={"Accept": "application/json"},
            timeout=self.budget.timeout_s,
            max_bytes=self.budget.max_bytes,
        )
        status = int(response.get("status", 200))
        final = response.get("final_url")
        if final and (urlsplit(str(final)).hostname or "").lower() != host:
            raise AcquisitionError(
                "network_policy", "provider redirected outside its host"
            )
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw or b"")
        if len(raw) > self.budget.max_bytes:
            raise AcquisitionError(
                "response_too_large", "provider response exceeds its byte budget"
            )
        self.budget.bytes += len(raw)
        self.budget.log.append(
            {
                "host": host,
                "path": urlsplit(url).path,
                "params": public,
                "status": status,
                "bytes": len(raw),
            }
        )
        self.raw.update(raw)
        if status == 404 and allow_404:
            return None
        if status == 429:
            raise AcquisitionError(
                "rate_limited", "provider rate limit reached (not retried)"
            )
        if status in (401, 403):
            raise AcquisitionError(
                "access_denied", f"provider refused access (HTTP {status})"
            )
        if status != 200:
            raise AcquisitionError(
                "provider_unavailable", f"provider returned HTTP {status}"
            )
        return raw


# ---------------------------------------------------------------------- Etherscan-compatible parsing


def etherscan_result(raw: bytes) -> Any:
    """The ``result`` of an Etherscan-compatible or proxy JSON-RPC response, or a classified error."""
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise AcquisitionError(
            "invalid_response", "provider response is not JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise AcquisitionError("invalid_response", "provider response is not an object")
    if "jsonrpc" in payload:
        if payload.get("error"):
            raise AcquisitionError("provider_error", "provider JSON-RPC error")
        return payload.get("result")
    result, message = payload.get("result"), str(payload.get("message") or "")
    if str(payload.get("status")) == "1":
        return result
    if result in (None, [], "") and message.lower().startswith("no "):
        # "No transactions found" / "No records found" / "No data found" (result null for a plain
        # account in getcontractcreation): an empty, successful answer.
        return []
    text = (str(result) + " " + message).lower()
    if "rate limit" in text:
        raise AcquisitionError(
            "rate_limited", "provider rate limit reached (not retried)"
        )
    if "api key" in text or "apikey" in text:
        raise AcquisitionError(
            "invalid_api_key", "provider rejected the configured API key"
        )
    raise AcquisitionError("provider_error", "provider returned an error status")


def _hex_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _status(row: Mapping[str, Any]) -> str | None:
    receipt, error = str(row.get("txreceipt_status", "")), str(row.get("isError", ""))
    if error == "1" or receipt == "0":
        return "failed"
    if receipt == "1" or (error == "0" and receipt == ""):
        return "success"
    return None


def parse_etherscan_txlist(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise AcquisitionError("invalid_response", "txlist result is a list")
    out = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("hash"):
            continue
        out.append(
            {
                "tx_hash": row["hash"],
                "block_number": _hex_int(row.get("blockNumber")),
                "block_hash": row.get("blockHash") or None,
                "timestamp": iso(_hex_int(row.get("timeStamp"))),
                "from": row.get("from") or None,
                "to": row.get("to") or None,
                "value": str(_hex_int(row.get("value")) or 0)
                if row.get("value") not in (None, "")
                else None,
                "status": _status(row),
                "confirmations": _hex_int(row.get("confirmations")),
                "contract_created": row.get("contractAddress") or None,
            }
        )
    return out


def parse_etherscan_tokentx(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise AcquisitionError("invalid_response", "tokentx result is a list")
    out = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("hash"):
            continue
        out.append(
            {
                "tx_hash": row["hash"],
                "token_contract": row.get("contractAddress") or None,
                "token_symbol": row.get("tokenSymbol") or None,
                "token_name": row.get("tokenName") or None,
                "token_decimals": row.get("tokenDecimal")
                if row.get("tokenDecimal") not in (None, "")
                else None,
                "from": row.get("from") or None,
                "to": row.get("to") or None,
                "value": row.get("value")
                if row.get("value") not in (None, "")
                else None,
                "block_number": _hex_int(row.get("blockNumber")),
                "block_hash": row.get("blockHash") or None,
                "timestamp": iso(_hex_int(row.get("timeStamp"))),
                "confirmations": _hex_int(row.get("confirmations")),
                "log_index": _hex_int(row.get("logIndex")),
                "standard": "erc20",
            }
        )
    return out


def parse_etherscan_creation(rows: Any) -> list[dict[str, Any]]:
    if rows in (None, []):
        return []
    if not isinstance(rows, list):
        raise AcquisitionError(
            "invalid_response", "getcontractcreation result is a list"
        )
    out = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("contractAddress"):
            continue
        timestamp = row.get("timestamp")
        out.append(
            {
                "contract_address": row["contractAddress"],
                "deployer": row.get("contractCreator") or None,
                "tx_hash": row.get("txHash") or None,
                "block_number": _hex_int(row.get("blockNumber")),
                "timestamp": iso(_hex_int(timestamp))
                if timestamp not in (None, "")
                else None,
                "factory": row.get("contractFactory") or None,
            }
        )
    return out


def first_inbound(
    address: str, transactions: list[dict[str, Any]], limit: int = FUNDING_INBOUND
) -> list[dict]:
    """The earliest successful inbound value transfers, in chain order."""
    target = address.lower()

    def receives(t: Mapping[str, Any]) -> bool:
        # A plain transfer names the address as ``to``; a creation transaction has no ``to`` and
        # names the new contract as ``contract_created``, so value sent with a deployment counts too.
        if t.get("to"):
            return t["to"].lower() == target
        return (
            bool(t.get("contract_created")) and t["contract_created"].lower() == target
        )

    rows = [
        t
        for t in transactions
        if receives(t)
        and t.get("status") != "failed"
        and int(t.get("value") or 0) > 0
        and t.get("block_number") is not None
    ]
    rows.sort(key=lambda t: (t["block_number"], t["tx_hash"]))
    return [
        {
            "tx_hash": t["tx_hash"],
            "from": t["from"],
            "value": t["value"],
            "block_number": t["block_number"],
            "timestamp": t.get("timestamp"),
        }
        for t in rows[:limit]
    ]


# ---------------------------------------------------------------------- Esplora parsing


def parse_esplora_tx(tx: Mapping[str, Any], head: int | None) -> dict[str, Any]:
    if not isinstance(tx, Mapping) or not tx.get("txid"):
        raise AcquisitionError("invalid_response", "an Esplora transaction has a txid")
    status = tx.get("status") or {}
    height = status.get("block_height") if status.get("confirmed") else None
    inputs = []
    for vin in tx.get("vin") or []:
        prevout = vin.get("prevout") or {}
        inputs.append(
            {
                "address": prevout.get("scriptpubkey_address"),
                "value": prevout.get("value"),
                "prev_tx": vin.get("txid"),
                "prev_index": vin.get("vout"),
                "coinbase": bool(vin.get("is_coinbase")),
            }
        )
    outputs = [
        {"address": o.get("scriptpubkey_address"), "value": o.get("value"), "index": n}
        for n, o in enumerate(tx.get("vout") or [])
    ]
    return {
        "tx_hash": tx["txid"],
        "block_number": height,
        "block_hash": status.get("block_hash"),
        "timestamp": iso(status.get("block_time"))
        if status.get("block_time") is not None
        else None,
        "status": "success" if height is not None else None,
        "confirmations": (head - height + 1)
        if height is not None and head is not None
        else None,
        "inputs": inputs,
        "outputs": outputs,
    }


def _json(raw: bytes | None, what: str) -> Any:
    try:
        return json.loads(raw or b"null")
    except ValueError as exc:
        raise AcquisitionError("invalid_response", f"{what} is not JSON") from exc


# ---------------------------------------------------------------------- label parsing


_LABEL_FIELDS = ("symbol", "type", "decimals", "website")


def parse_token_label(raw: bytes, address: str) -> dict[str, Any]:
    """One ethereum-lists token file quoted as a label assertion; person-linked fields are dropped."""
    payload = _json(raw, "token file")
    if (
        not isinstance(payload, Mapping)
        or not payload.get("address")
        or not payload.get("name")
    ):
        raise AcquisitionError("invalid_response", "token file has no address or name")
    if normalize_address(ETHEREUM, payload["address"]) != address:
        raise AcquisitionError(
            "invalid_response", "token file names a different address"
        )
    attributes = {
        k: payload[k] for k in _LABEL_FIELDS if payload.get(k) not in (None, "")
    }
    return {
        "address": address,
        "label_id": "token",
        "label": str(payload["name"]),
        "category": payload.get("type") or None,
        "attributes": attributes,
        "source_record": f"src/tokens/eth/{checksum_address(address)}.json",
    }


# ---------------------------------------------------------------------- core


def _now_ms() -> int:
    return int(time.time() * 1000)


def _secret(source: Mapping[str, Any], api_key: str | None) -> str | None:
    auth = source["auth"]
    if auth.get("kind") == "none":
        return None
    return api_key or os.environ.get(str(auth.get("secret_ref") or "")) or None


def _acquire(
    conn: Any,
    namespace: str,
    *,
    source_id: str,
    operation: str,
    chain: str,
    subject: Mapping[str, str],
    controls: Mapping[str, Any],
    run: Callable[[Session, dict[str, Any]], dict[str, Any]],
    request_id: str,
    principal_id: str,
    scopes: Any,
    transport: Callable[..., Any] | None,
    api_key: str | None,
    max_requests: int,
    max_bytes: int | None,
    timeout_s: float | None,
    now: Callable[[], int] | None,
    investigation: str | None,
) -> dict[str, Any]:
    authorize(namespace, scopes, WRITE_SCOPE)
    source = load_source(source_id)
    secret = _secret(source, api_key)
    if source["auth"].get("kind") == "required-secret" and not secret:
        return {
            "status": "no_provider_configured",
            "source_id": source_id,
            "operation": operation,
            "note": f"set {source['auth'].get('secret_ref')} to enable this source; nothing was requested or "
            "written",
        }
    budgets = source["budgets"]
    max_bytes = int(max_bytes or budgets["max_bytes"])
    timeout_s = float(timeout_s or budgets["timeout_ms"] / 1000)
    if (
        not str(request_id or "").strip()
        or not 1 <= max_bytes <= int(budgets["max_bytes"])
        or not 0 < timeout_s <= budgets["timeout_ms"] / 1000
    ):
        raise AcquisitionError("invalid_controls", "invalid acquisition controls")
    endpoint = str(source["endpoint"])
    public_controls = json.loads(canonical(dict(controls)))
    key = digest(
        [
            namespace,
            source_id,
            operation,
            chain,
            dict(subject),
            public_controls,
            max_bytes,
            timeout_s,
            max_requests,
            ADAPTER_VERSION,
        ]
    )
    store = OnchainStore(conn)
    prior = conn.execute(
        "SELECT input_hash, receipt_json FROM onchain_acquisitions WHERE request_id=?",
        [request_id],
    ).fetchone()
    if prior:
        if prior[0] != key:
            raise AcquisitionError(
                "request_id_reused", "request ID already used with different inputs"
            )
        return json.loads(prior[1])
    clock = now or _now_ms
    started = clock()
    license_ = dict(source.get("license") or {})
    budget = AcquisitionBudget(
        allowlist=((urlsplit(endpoint).hostname or "").lower(),),
        max_requests=max_requests,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
    )
    if transport is None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        transport = HTTPSPageAdapter._request
    session = Session(source, budget, transport, secret)
    receipt: dict[str, Any] = {
        "request_id": request_id,
        "input_hash": key,
        "namespace": namespace,
        "source_id": source_id,
        "operation": operation,
        "chain_id": chain,
        "subject": dict(subject),
        "controls": public_controls,
        "endpoint": endpoint,
        "retrieved_at_ms": started,
        "adapter_version": ADAPTER_VERSION,
        "max_bytes": max_bytes,
        "timeout_s": timeout_s,
        "max_requests": max_requests,
        "retries": 0,
        "credential": "configured" if secret else "not_required",
        "redistribution": license_.get("redistribution"),
        "license": license_,
    }
    try:
        facts = run(session, {"endpoint": endpoint, "source": source})
        status = facts.pop("_status", "acquired")
        observation_id = (
            "onchain-obs:"
            + digest(
                [
                    namespace,
                    request_id,
                    source_id,
                    operation,
                    chain,
                    dict(subject),
                    started,
                    digest(facts),
                ]
            )[:28]
        )
        record = {
            "contract": CONTRACT,
            "observation_id": observation_id,
            "namespace": namespace,
            "provider": source_id,
            "publisher": source.get("publisher"),
            "dataset": (source.get("policy") or {}).get("dataset"),
            "attribution": (source.get("policy") or {}).get("attribution"),
            "operation": operation,
            "chain_id": chain,
            "subject": dict(subject),
            "observed_at": datetime.fromtimestamp(started / 1000, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "observed_at_ms": started,
            "temporal_semantics": "ledger facts as reported at the stated chain head",
            "locator": {"endpoint": endpoint, "requests": budget.log},
            "raw_sha256": session.raw.hexdigest(),
            "raw_stored": False,
            "license": license_,
            "request_id": request_id,
            "facts": facts,
        }
        conn.execute("BEGIN TRANSACTION")
        try:
            store.record_observation(namespace, record)
            projected = store.project(
                namespace, record, principal_id=principal_id, scopes=scopes
            )
            receipt.update(
                status=status,
                observation_id=observation_id,
                requests=budget.used,
                bytes=budget.bytes,
                added=projected["added"],
                changes=[
                    {
                        k: c.get(k)
                        for k in ("kind", "record_key", "change", "revision", "current")
                    }
                    for c in projected["changes"]
                ],
            )
            conn.execute(
                "INSERT INTO onchain_acquisitions VALUES (?,?,?,?,?,?)",
                [request_id, namespace, source_id, operation, key, canonical(receipt)],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except (OnchainError, IdentifierError) as exc:
        receipt.update(
            status="failed",
            failure_type=exc.code,
            requests=budget.used,
            bytes=budget.bytes,
        )
        conn.execute(
            "INSERT INTO onchain_acquisitions VALUES (?,?,?,?,?,?)",
            [request_id, namespace, source_id, operation, key, canonical(receipt)],
        )
    except Exception as exc:  # noqa: BLE001 - persist bounded failure diagnostics, never retry
        receipt.update(
            status="failed",
            failure_type=getattr(exc, "code", type(exc).__name__),
            requests=budget.used,
            bytes=budget.bytes,
        )
        conn.execute(
            "INSERT INTO onchain_acquisitions VALUES (?,?,?,?,?,?)",
            [request_id, namespace, source_id, operation, key, canonical(receipt)],
        )
    if investigation:
        record_in_investigation(conn, investigation, receipt)
    return receipt


def record_in_investigation(
    conn: Any, investigation: str, receipt: Mapping[str, Any]
) -> int | None:
    """Log an acquisition receipt into an investigation's provisioning audit trail."""
    from src.provisioning import store

    if not store.schema_ready(conn) or store.get_kg(conn, investigation) is None:
        return None
    detail = {
        k: receipt.get(k)
        for k in (
            "request_id",
            "source_id",
            "operation",
            "chain_id",
            "subject",
            "status",
            "observation_id",
            "redistribution",
            "retrieved_at_ms",
        )
    }
    return store.record_event(
        conn, investigation, "onchain-observation", detail, datetime.now(UTC)
    )


def _pages(
    source: Mapping[str, Any], page_size: int | None, max_pages: int | None
) -> tuple[int, int]:
    budgets = source["budgets"]
    size = int(page_size or min(100, budgets["max_results"]))
    pages = int(max_pages or budgets["max_pages"])
    if not 1 <= size <= int(budgets["max_results"]) or not 1 <= pages <= int(
        budgets["max_pages"]
    ):
        raise AcquisitionError(
            "invalid_controls", "page size and pages must stay within the source budget"
        )
    if size * pages > 10_000:
        raise AcquisitionError("invalid_controls", "page*offset may not exceed 10,000")
    return size, pages


def _rpc(session: Session, endpoint: str, method: str, **params: Any) -> Any:
    if method not in READ_RPC_METHODS:
        raise AcquisitionError(
            "method_refused", "only read-only JSON-RPC methods are sent"
        )
    return etherscan_result(
        session.get(
            endpoint, {"chainid": 1, "module": "proxy", "action": method, **params}
        )
    )


def _eth_head(session: Session, endpoint: str) -> int:
    head = _hex_int(_rpc(session, endpoint, "eth_blockNumber"))
    if head is None:
        raise AcquisitionError("invalid_response", "eth_blockNumber returned no height")
    return head


def _etherscan_list(
    session: Session,
    endpoint: str,
    action: str,
    address: str,
    start: int,
    end: int | None,
    size: int,
    pages: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    parse = parse_etherscan_txlist if action == "txlist" else parse_etherscan_tokentx
    for page in range(1, pages + 1):
        params = {
            "chainid": 1,
            "module": "account",
            "action": action,
            "address": address,
            "startblock": start,
            "endblock": end if end is not None else 99_999_999,
            "page": page,
            "offset": size,
            "sort": "asc",
        }
        result = etherscan_result(session.get(endpoint, params))
        batch = parse(result)
        rows += batch
        if len(result) < size:
            return rows, False
    return rows, True


def _complete_to(rows: list[dict[str, Any]], truncated: bool, end: int) -> int:
    if not truncated:
        return end
    return (
        max(
            (r["block_number"] for r in rows if r.get("block_number") is not None),
            default=0,
        )
        - 1
    )


def acquire_address_transactions(
    conn: Any,
    namespace: str,
    chain_id: str,
    address: str,
    *,
    request_id: str,
    principal_id: str,
    scopes: Any,
    from_block: int | None = None,
    to_block: int | None = None,
    page_size: int | None = None,
    max_pages: int | None = None,
    transport: Callable[..., Any] | None = None,
    api_key: str | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """Transactions (and, on Ethereum, token transfers) of one explicit address in a block range."""
    chain = normalize_chain(chain_id)
    addr = normalize_address(chain, address)
    source = load_source(SOURCE_FOR_CHAIN[chain])
    size, pages = _pages(source, page_size, max_pages)
    start = max(0, int(from_block or 0))
    end = None if to_block is None else int(to_block)
    if end is not None and end < start:
        raise AcquisitionError("invalid_controls", "to_block precedes from_block")

    def run_evm(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        endpoint = ctx["endpoint"]
        head = _eth_head(session, endpoint)
        upper = head if end is None else min(end, head)
        txs, tx_trunc = _etherscan_list(
            session, endpoint, "txlist", addr, start, upper, size, pages
        )
        transfers, tr_trunc = _etherscan_list(
            session, endpoint, "tokentx", addr, start, upper, size, pages
        )
        complete_to = min(
            _complete_to(txs, tx_trunc, upper), _complete_to(transfers, tr_trunc, upper)
        )
        coverage = []
        if complete_to >= start:
            coverage.append(
                {
                    "address": addr,
                    "operation": "address-transactions",
                    "from_block": start,
                    "to_block": complete_to,
                    "complete": True,
                }
            )
        if complete_to < upper:
            coverage.append(
                {
                    "address": addr,
                    "operation": "address-transactions",
                    "from_block": max(start, complete_to + 1),
                    "to_block": upper,
                    "complete": False,
                }
            )
        return {
            "head_block": head,
            "transactions": txs,
            "transfers": transfers,
            "coverage": coverage,
            "window": {"from_block": start, "to_block": upper},
            "truncated": tx_trunc or tr_trunc,
        }

    def run_utxo(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        base = ctx["endpoint"].rstrip("/")
        head = _hex_int(session.get(f"{base}/blocks/tip/height").decode().strip())
        if head is None:
            raise AcquisitionError("invalid_response", "tip height is not a number")
        upper = head if end is None else min(end, head)
        txs: list[dict[str, Any]] = []
        last, exhausted, passed = None, False, False
        for _ in range(pages):
            url = f"{base}/address/{addr}/txs/chain" + (f"/{last}" if last else "")
            batch = _json(session.get(url), "address transactions")
            if not isinstance(batch, list):
                raise AcquisitionError(
                    "invalid_response", "address transactions are a list"
                )
            parsed = [parse_esplora_tx(tx, head) for tx in batch]
            txs += parsed
            if len(batch) < ESPLORA_PAGE:
                exhausted = True
                break
            last = batch[-1]["txid"]
            oldest = min(
                (t["block_number"] for t in parsed if t["block_number"] is not None),
                default=None,
            )
            if oldest is not None and oldest < start:
                passed = True
                break
        kept = [
            t
            for t in txs
            if t["block_number"] is not None and start <= t["block_number"] <= upper
        ]
        oldest = min(
            (t["block_number"] for t in txs if t["block_number"] is not None),
            default=start,
        )
        complete_from = start if (exhausted or passed) else max(start, oldest + 1)
        coverage = []
        if complete_from <= upper:
            coverage.append(
                {
                    "address": addr,
                    "operation": "address-transactions",
                    "from_block": complete_from,
                    "to_block": upper,
                    "complete": True,
                }
            )
        if complete_from > start:
            coverage.append(
                {
                    "address": addr,
                    "operation": "address-transactions",
                    "from_block": start,
                    "to_block": complete_from - 1,
                    "complete": False,
                }
            )
        return {
            "head_block": head,
            "transactions": kept,
            "coverage": coverage,
            "window": {"from_block": start, "to_block": upper},
            "truncated": not (exhausted or passed),
            "mempool": "not_acquired",
        }

    evm = family(chain) == "evm"
    return _acquire(
        conn,
        namespace,
        source_id=source["source_id"],
        operation="address-transactions",
        chain=chain,
        subject={"kind": "address", "value": addr},
        controls={
            "from_block": start,
            "to_block": end,
            "page_size": size,
            "max_pages": pages,
        },
        run=run_evm if evm else run_utxo,
        request_id=request_id,
        principal_id=principal_id,
        scopes=scopes,
        transport=transport,
        api_key=api_key,
        max_requests=(1 + 2 * pages) if evm else (1 + pages),
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        investigation=investigation,
    )


def _eth_transaction(
    session: Session, endpoint: str, tx_hash: str, head: int
) -> dict[str, Any] | None:
    tx = _rpc(session, endpoint, "eth_getTransactionByHash", txhash=tx_hash)
    if tx is None:
        return None
    if not isinstance(tx, Mapping):
        raise AcquisitionError("invalid_response", "transaction result is an object")
    block = _hex_int(tx.get("blockNumber"))
    status, created, block_hash, timestamp = None, None, tx.get("blockHash"), None
    if block is not None:
        receipt = (
            _rpc(session, endpoint, "eth_getTransactionReceipt", txhash=tx_hash) or {}
        )
        status = {"0x1": "success", "0x0": "failed"}.get(
            str(receipt.get("status", "")).lower()
        )
        created = receipt.get("contractAddress") or None
        header = (
            _rpc(
                session,
                endpoint,
                "eth_getBlockByNumber",
                tag=hex(block),
                boolean="false",
            )
            or {}
        )
        timestamp = (
            iso(_hex_int(header.get("timestamp"))) if header.get("timestamp") else None
        )
        block_hash = header.get("hash") or block_hash
    return {
        "tx_hash": tx.get("hash") or tx_hash,
        "block_number": block,
        "block_hash": block_hash,
        "timestamp": timestamp,
        "from": tx.get("from"),
        "to": tx.get("to") or None,
        "value": str(_hex_int(tx.get("value")) or 0),
        "status": status,
        "confirmations": (head - block + 1) if block is not None else None,
        "contract_created": created,
    }


def acquire_transaction(
    conn: Any,
    namespace: str,
    chain_id: str,
    tx_hash: str,
    *,
    request_id: str,
    principal_id: str,
    scopes: Any,
    transport: Callable[..., Any] | None = None,
    api_key: str | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """One transaction by hash; a transaction the explorer no longer finds is recorded as ``dropped``."""
    chain = normalize_chain(chain_id)
    txid = normalize_tx_hash(chain, tx_hash)
    source = load_source(SOURCE_FOR_CHAIN[chain])

    def run_evm(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        head = _eth_head(session, ctx["endpoint"])
        tx = _eth_transaction(session, ctx["endpoint"], txid, head)
        if tx is None:
            return {"head_block": head, "dropped": [txid], "_status": "not_found"}
        return {"head_block": head, "transactions": [tx]}

    def run_utxo(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        base = ctx["endpoint"].rstrip("/")
        head = _hex_int(session.get(f"{base}/blocks/tip/height").decode().strip())
        raw = session.get(f"{base}/tx/{txid}", allow_404=True)
        if raw is None:
            return {"head_block": head, "dropped": [txid], "_status": "not_found"}
        return {
            "head_block": head,
            "transactions": [parse_esplora_tx(_json(raw, "transaction"), head)],
        }

    evm = family(chain) == "evm"
    return _acquire(
        conn,
        namespace,
        source_id=source["source_id"],
        operation="transaction",
        chain=chain,
        subject={"kind": "tx", "value": txid},
        controls={},
        run=run_evm if evm else run_utxo,
        request_id=request_id,
        principal_id=principal_id,
        scopes=scopes,
        transport=transport,
        api_key=api_key,
        max_requests=4 if evm else 2,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        investigation=investigation,
    )


def _funding_fact(
    session: Session,
    endpoint: str,
    address: str,
    start: int,
    head: int,
    size: int,
    subject_kind: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    params = {
        "chainid": 1,
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": start,
        "endblock": head,
        "page": 1,
        "offset": size,
        "sort": "asc",
    }
    result = etherscan_result(session.get(endpoint, params))
    rows = parse_etherscan_txlist(result)
    first = first_inbound(address, rows)
    full = len(result) >= size
    fact = {
        "address": address,
        "subject_kind": subject_kind,
        "first_inbound": first,
        "window": {
            "from_block": start,
            "to_block": head
            if not full
            else max(
                (r["block_number"] for r in rows if r.get("block_number") is not None),
                default=start,
            ),
        },
        "complete": bool(first) or not full,
        "internal_transfers": "not_acquired",
    }
    kept = {f["tx_hash"] for f in first}
    return fact, [r for r in rows if r["tx_hash"] in kept]


def acquire_address_funding(
    conn: Any,
    namespace: str,
    chain_id: str,
    address: str,
    *,
    request_id: str,
    principal_id: str,
    scopes: Any,
    page_size: int | None = None,
    max_pages: int | None = None,
    transport: Callable[..., Any] | None = None,
    api_key: str | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """The first inbound value transfers of one explicit address (one hop of a funding chain)."""
    chain = normalize_chain(chain_id)
    addr = normalize_address(chain, address)
    source = load_source(SOURCE_FOR_CHAIN[chain])
    size, pages = _pages(source, page_size, max_pages)

    def run_evm(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        head = _eth_head(session, ctx["endpoint"])
        fact, txs = _funding_fact(
            session, ctx["endpoint"], addr, 0, head, size, "account"
        )
        return {"head_block": head, "funding": [fact], "transactions": txs}

    def run_utxo(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        base = ctx["endpoint"].rstrip("/")
        head = _hex_int(session.get(f"{base}/blocks/tip/height").decode().strip())
        txs: list[dict[str, Any]] = []
        last, exhausted = None, False
        for _ in range(pages):
            batch = _json(
                session.get(
                    f"{base}/address/{addr}/txs/chain" + (f"/{last}" if last else "")
                ),
                "txs",
            )
            if not isinstance(batch, list):
                raise AcquisitionError(
                    "invalid_response", "address transactions are a list"
                )
            txs += [parse_esplora_tx(tx, head) for tx in batch]
            if len(batch) < ESPLORA_PAGE:
                exhausted = True
                break
            last = batch[-1]["txid"]
        inbound = []
        for tx in sorted(
            (t for t in txs if t["block_number"] is not None),
            key=lambda t: (t["block_number"], t["tx_hash"]),
        ):
            received = sum(
                int(o["value"] or 0) for o in tx["outputs"] if o["address"] == addr
            )
            if received > 0 and not any(i["address"] == addr for i in tx["inputs"]):
                senders = sorted({i["address"] for i in tx["inputs"] if i["address"]})
                inbound.append(
                    {
                        "tx_hash": tx["tx_hash"],
                        "from": senders[0] if len(senders) == 1 else None,
                        "value": str(received),
                        "block_number": tx["block_number"],
                        "timestamp": tx["timestamp"],
                        "via": "utxo-output",
                    }
                )
        first = inbound[:FUNDING_INBOUND] if exhausted else []
        oldest = min(
            (t["block_number"] for t in txs if t["block_number"] is not None), default=0
        )
        fact = {
            "address": addr,
            "subject_kind": "account",
            "first_inbound": first,
            "window": {"from_block": 0 if exhausted else oldest, "to_block": head},
            "complete": exhausted,
            "internal_transfers": "not_applicable",
        }
        kept = {f["tx_hash"] for f in first}
        return {
            "head_block": head,
            "funding": [fact],
            "transactions": [t for t in txs if t["tx_hash"] in kept],
        }

    evm = family(chain) == "evm"
    return _acquire(
        conn,
        namespace,
        source_id=source["source_id"],
        operation="address-funding",
        chain=chain,
        subject={"kind": "address", "value": addr},
        controls={"page_size": size, "max_pages": pages},
        run=run_evm if evm else run_utxo,
        request_id=request_id,
        principal_id=principal_id,
        scopes=scopes,
        transport=transport,
        api_key=api_key,
        max_requests=2 if evm else 1 + pages,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        investigation=investigation,
    )


def acquire_contract_origin(
    conn: Any,
    namespace: str,
    contract: str,
    *,
    request_id: str,
    principal_id: str,
    scopes: Any,
    chain_id: str = ETHEREUM,
    page_size: int | None = None,
    transport: Callable[..., Any] | None = None,
    api_key: str | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """Deployment of one explicit contract, its first inbound funding and its deployer's first funding."""
    chain = normalize_chain(chain_id)
    if family(chain) != "evm":
        raise AcquisitionError(
            "unsupported_chain", "contract origin is an EVM operation"
        )
    addr = normalize_address(chain, contract)
    source = load_source(SOURCE_FOR_CHAIN[chain])
    size, _ = _pages(source, page_size, 1)

    def run(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        endpoint = ctx["endpoint"]
        head = _eth_head(session, endpoint)
        created = parse_etherscan_creation(
            etherscan_result(
                session.get(
                    endpoint,
                    {
                        "chainid": 1,
                        "module": "contract",
                        "action": "getcontractcreation",
                        "contractaddresses": addr,
                    },
                )
            )
        )
        mine = [
            c
            for c in created
            if normalize_address(chain, c["contract_address"]) == addr
        ]
        if not mine:
            return {
                "head_block": head,
                "_status": "not_a_contract",
                "coverage": [
                    {
                        "address": addr,
                        "operation": "contract-origin",
                        "from_block": 0,
                        "to_block": head,
                        "complete": True,
                    }
                ],
            }
        dep = dict(mine[0])
        transactions: list[dict[str, Any]] = []
        if dep.get("tx_hash") and (
            dep.get("block_number") is None or dep.get("timestamp") is None
        ):
            tx = _eth_transaction(
                session, endpoint, normalize_tx_hash(chain, dep["tx_hash"]), head
            )
            if tx is not None:
                dep["block_number"] = (
                    dep.get("block_number")
                    if dep.get("block_number") is not None
                    else tx["block_number"]
                )
                dep["timestamp"] = dep.get("timestamp") or tx["timestamp"]
                transactions.append(tx)
        start = int(dep.get("block_number") or 0)
        contract_funding, txs = _funding_fact(
            session, endpoint, addr, start, head, size, "contract"
        )
        transactions += txs
        funding = [contract_funding]
        if dep.get("deployer"):
            deployer = normalize_address(chain, dep["deployer"])
            deployer_funding, txs = _funding_fact(
                session, endpoint, deployer, 0, head, size, "account"
            )
            funding.append(deployer_funding)
            transactions += txs
        return {
            "head_block": head,
            "deployments": [dep],
            "funding": funding,
            "transactions": transactions,
            "coverage": [
                {
                    "address": addr,
                    "operation": "contract-origin",
                    "from_block": 0,
                    "to_block": head,
                    "complete": True,
                }
            ],
        }

    return _acquire(
        conn,
        namespace,
        source_id=source["source_id"],
        operation="contract-origin",
        chain=chain,
        subject={"kind": "contract", "value": addr},
        controls={"page_size": size},
        run=run,
        request_id=request_id,
        principal_id=principal_id,
        scopes=scopes,
        transport=transport,
        api_key=api_key,
        max_requests=7,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        investigation=investigation,
    )


def acquire_label(
    conn: Any,
    namespace: str,
    address: str,
    *,
    request_id: str,
    principal_id: str,
    scopes: Any,
    chain_id: str = ETHEREUM,
    transport: Callable[..., Any] | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """What the ethereum-lists token dataset states about one explicit contract address, quoted with attribution."""
    chain = normalize_chain(chain_id)
    if chain != ETHEREUM:
        raise AcquisitionError(
            "unsupported_chain",
            "the selected label dataset covers Ethereum mainnet only",
        )
    addr = normalize_address(chain, address)

    def run(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
        url = ctx["endpoint"].rstrip("/") + f"/{checksum_address(addr)}.json"
        raw = session.get(url, allow_404=True)
        coverage = [
            {
                "address": addr,
                "operation": "label",
                "from_block": None,
                "to_block": None,
                "complete": True,
            }
        ]
        if raw is None:
            # A label the source stated earlier and no longer states becomes a withdrawn revision.
            return {
                "labels": [],
                "labels_withdrawn": [addr],
                "coverage": coverage,
                "_status": "not_labelled_by_source",
            }
        return {"labels": [parse_token_label(raw, addr)], "coverage": coverage}

    return _acquire(
        conn,
        namespace,
        source_id=TOKEN_LABELS,
        operation="label",
        chain=chain,
        subject={"kind": "address", "value": addr},
        controls={},
        run=run,
        request_id=request_id,
        principal_id=principal_id,
        scopes=scopes,
        transport=transport,
        api_key=None,
        max_requests=1,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        investigation=investigation,
    )


# ---------------------------------------------------------------------- fixtures


def fixture_transport(responses: Mapping[str, Any]) -> Callable[..., dict[str, Any]]:
    """A deterministic transport over captured or authored native responses.

    ``responses`` maps a request key (``path`` plus sorted public parameters,
    see :func:`request_key`) to ``{"status": int, "body": <json or text>}``.
    Unknown requests answer 404. The secret parameter is never part of a key.
    """

    def transport(
        *,
        url: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> dict[str, Any]:
        entry = responses.get(request_key(url, params))
        if entry is None:
            return {"status": 404, "content": b"", "final_url": url}
        body = entry.get("body")
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return {
            "status": int(entry.get("status", 200)),
            "content": raw,
            "final_url": url,
        }

    return transport


def request_key(url: str, params: Mapping[str, Any]) -> str:
    parts = urlsplit(url)
    public = sorted(
        (str(k), str(v)) for k, v in params.items() if k not in {"apikey", "api_key"}
    )
    return (
        parts.hostname
        + parts.path
        + ("?" + "&".join(f"{k}={v}" for k, v in public) if public else "")
    )
