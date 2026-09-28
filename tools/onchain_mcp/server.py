"""
Noesis On-chain Observations - MCP server (#2053, B05 #2058).

Read-only, honesty-wrapped tools over the cited on-chain records of
``src/kb/onchain.py``:

  address_observations(address, chain_id?, from_block?, to_block?)
      -> cited transactions and token transfers in and out of one address,
         with coverage and explicit unknowns
  contract_origin(contract)
      -> cited deployer, the contract's first inbound funding and the
         deployer's first-funding chain; unacquired hops stay unknown
  address_cluster(address, chain_id?)
      -> addresses probably controlled together, every edge with its declared
         heuristic, citations, the calibrated threshold, measured FPR and a
         null model; never an owner or attribution

Nothing here acquires, signs, submits or attributes. Acquisition is the
receipted Python API in ``src/ingestion/onchain.py``. Every invocation is
logged to a provisioning audit trail kept in a separate store (never the
warehouse), like the OSINT gated tools. Person attribution, wallet or key
access, transaction submission and exchange/KYC data are pack exclusions and
have no tool.

Design constraints (as for every tool server): stdlib + fastmcp (plus the
stdlib-only honesty helper) at import time, lazy imports inside tools, the
warehouse opened READ-ONLY.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analytics.honesty import honesty_output_schema  # noqa: E402

mcp = FastMCP("noesis-onchain")

READ_SCOPE = "knowledge:onchain:read"
# Scopes each tool always needs; tests assert the catalog and descriptor declare exactly these.
TOOL_SCOPES = {
    "address_observations": [READ_SCOPE],
    "contract_origin": [READ_SCOPE],
    "address_cluster": [READ_SCOPE],
}
# Pack exclusions: never served, whatever flag is set. A test asserts their absence.
EXCLUDED_TOOLS = (
    "attribute_address",
    "address_owner",
    "deanonymize_address",
    "wallet_balance_for_person",
    "sign_transaction",
    "send_transaction",
    "submit_transaction",
    "import_private_key",
    "exchange_kyc_lookup",
)
DEFAULT_NAMESPACE = "onchain"


def _warehouse_path() -> str:
    from src.config.env import warehouse_path

    return warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))


def _warehouse_ro():
    import duckdb

    path = _warehouse_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


def _context() -> tuple:
    """Caller principal and scopes (access token, else NOESIS_MCP_PRINCIPAL / NOESIS_MCP_SCOPES)."""
    from fastmcp.server.dependencies import get_access_token

    from src.config.env import resolve_env

    token = get_access_token()
    if token is not None and token.scopes:
        return str(token.client_id or ""), set(token.scopes)
    principal = (resolve_env("MCP_PRINCIPAL", "local-reader") or "").strip()
    raw = resolve_env("MCP_SCOPES", "knowledge:read") or ""
    return principal, {value.strip() for value in raw.split(",") if value.strip()}


def _audit_path() -> str:
    """The separate audit store: ``NOESIS_ONCHAIN_AUDIT_PATH`` or a file beside the warehouse."""
    from src.config.env import resolve_env

    configured = resolve_env("ONCHAIN_AUDIT_PATH")
    if configured:
        return configured
    return str(Path(_warehouse_path()).with_name("onchain_audit.duckdb"))


def _log(
    tool: str,
    principal: str,
    arguments: dict,
    result: dict,
    investigation: Optional[str],
) -> None:
    """Every invocation goes to the provisioning audit trail in the separate audit store."""
    try:
        import duckdb

        from src.provisioning import store

        conn = duckdb.connect(_audit_path())
        try:
            store.ensure_schema(conn)
            store.record_event(
                conn,
                investigation or "onchain-observations",
                f"onchain.{tool}",
                {
                    "principal": principal,
                    "arguments": arguments,
                    "status": result.get("status"),
                    "generation": result.get("generation"),
                },
                datetime.now(UTC),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - auditing must not turn into a side channel for errors
        pass


def _unauthorized(tool: str) -> dict:
    return {
        "status": "unauthorized",
        "required_scopes": TOOL_SCOPES[tool],
        "n": 0,
        "method": "authorization",
        "assumptions": [],
    }


def _run(tool: str, arguments: dict, investigation: Optional[str], call) -> dict:
    principal, scopes = _context()
    if not set(TOOL_SCOPES[tool]) <= scopes and "operator" not in scopes:
        result = _unauthorized(tool)
    else:
        try:
            con = _warehouse_ro()
        except Exception as exc:
            result = {
                "status": "not_ready",
                "error": str(exc),
                "n": 0,
                "method": "stored explorer observations",
                "assumptions": [],
            }
        else:
            try:
                result = call(con)
            except Exception as exc:  # noqa: BLE001 - a tool never raises through MCP
                result = {"status": "error", "error": str(exc)}
            finally:
                con.close()
    result.setdefault("n", 0)
    result.setdefault("method", "stored explorer observations")
    result.setdefault("assumptions", [])
    _log(tool, principal, arguments, result, investigation)
    return result


_OBSERVATION_FIELDS = {
    "status": {"type": "string"},
    "address": {"type": "string"},
    "chain_id": {"type": "string"},
    "items": {"type": "array"},
    "counterparties": {"type": "array"},
    "labels": {"type": "object"},
    "coverage": {"type": "array"},
    "unknowns": {"type": "array"},
    "generation": {"type": "string"},
}


@mcp.tool(output_schema=honesty_output_schema(_OBSERVATION_FIELDS))
def address_observations(
    address: str,
    chain_id: str = "eip155:1",
    from_block: Optional[int] = None,
    to_block: Optional[int] = None,
    namespace: str = DEFAULT_NAMESPACE,
    investigation: Optional[str] = None,
) -> dict:
    """Cited transactions and token transfers in and out of one explicit ledger
    address within a block range, each with block height, timestamp and an
    explorer citation. Block ranges no acquisition covered, truncated windows
    and unacquired internal transfers are listed as unknowns; an address no
    acquisition ever covered returns ``status: unknown``. Labels are quoted
    from their sources; nothing attributes the address to anyone.

    Args:
        address: an EVM address (0x...) or a Bitcoin address; names are refused.
        chain_id: CAIP-2 chain id (eip155:1 or bip122:000000000019d6689c085ae165831e93).
        from_block: first block of the range (inclusive).
        to_block: last block of the range (inclusive).
        namespace: the record namespace to read.
        investigation: optional investigation name for the audit trail.
    """
    from src.kb.onchain import address_observations as _observations

    arguments = {
        "address": address,
        "chain_id": chain_id,
        "from_block": from_block,
        "to_block": to_block,
        "namespace": namespace,
    }
    return _run(
        "address_observations",
        arguments,
        investigation,
        lambda con: _observations(
            con, namespace, chain_id, address, from_block=from_block, to_block=to_block
        ),
    )


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "status": {"type": "string"},
            "contract": {"type": "string"},
            "deployment": {"type": ["object", "null"]},
            "contract_funding": {"type": "object"},
            "deployer_funding_chain": {"type": "array"},
            "labels": {"type": "object"},
            "unknowns": {"type": "array"},
            "generation": {"type": "string"},
        }
    )
)
def contract_origin(
    contract: str,
    chain_id: str = "eip155:1",
    max_hops: int = 3,
    namespace: str = DEFAULT_NAMESPACE,
    investigation: Optional[str] = None,
) -> dict:
    """The cited deployer and creation transaction of one contract, its first
    inbound funding, and the deployer's first-funding chain (each hop the
    earliest inbound value transfer of the previous address). A hop that was
    never acquired is reported as ``not_acquired``, never guessed. A funder is
    a ledger fact, not a controller or owner.

    Args:
        contract: the contract address (0x...).
        chain_id: CAIP-2 chain id of an EVM chain (eip155:1).
        max_hops: funding-chain hops to follow (1-5).
        namespace: the record namespace to read.
        investigation: optional investigation name for the audit trail.
    """
    from src.kb.onchain import contract_origin as _origin

    arguments = {
        "contract": contract,
        "chain_id": chain_id,
        "max_hops": max_hops,
        "namespace": namespace,
    }
    return _run(
        "contract_origin",
        arguments,
        investigation,
        lambda con: _origin(con, namespace, chain_id, contract, max_hops=max_hops),
    )


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "status": {"type": "string"},
            "address": {"type": "string"},
            "members": {"type": "array"},
            "edges": {"type": "array"},
            "heuristics": {"type": "object"},
            "threshold": {"type": "object"},
            "null_model": {"type": "object"},
            "note": {"type": "string"},
            "labels": {"type": "object"},
            "generation": {"type": "string"},
        }
    )
)
def address_cluster(
    address: str,
    chain_id: str = "eip155:1",
    min_score: Optional[float] = None,
    max_depth: int = 2,
    max_edges: int = 50,
    namespace: str = DEFAULT_NAMESPACE,
    investigation: Optional[str] = None,
) -> dict:
    """Addresses probably controlled together with one address (status
    ``probable``): every edge names its heuristic (common-input,
    deposit-address-reuse, deployer-funding) and cites the transactions that
    establish it; the result carries the calibrated threshold with its
    measured false-positive rate on a labelled synthetic fixture, a
    degree-preserving null model and a caveat that exchange, bridge and mixer
    activity commonly breaks clustering. There is no owner or attribution
    field. Refuses to run without a cited transaction for the address.

    Args:
        address: an EVM or Bitcoin address.
        chain_id: CAIP-2 chain id.
        min_score: edge threshold (0-1]; the calibrated default when omitted.
        max_depth: traversal depth (1-3).
        max_edges: edge budget (1-200).
        namespace: the record namespace to read.
        investigation: optional investigation name for the audit trail.
    """
    from src.kb.onchain_clustering import address_cluster as _cluster

    arguments = {
        "address": address,
        "chain_id": chain_id,
        "min_score": min_score,
        "max_depth": max_depth,
        "max_edges": max_edges,
        "namespace": namespace,
    }
    return _run(
        "address_cluster",
        arguments,
        investigation,
        lambda con: _cluster(
            con,
            namespace,
            chain_id,
            address,
            min_score=min_score,
            max_depth=max_depth,
            max_edges=max_edges,
        ),
    )


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
