"""Calibration of the address-clustering threshold (#2053, B04 #2057).

In the style of :mod:`src.osint.gated_calibration`: :func:`calibrate` sweeps
``min_score`` over a labelled synthetic fixture
(``config/onchain/cluster-calibration.json``) of known-related and coincidental
address pairs and reports the false-positive and true-positive rates per
threshold. The fixture deliberately contains the known false-positive cases:
exchange hot wallets and bridge contracts that receive from many unrelated
users, an exchange withdrawal wallet funding unrelated deployers, a
PayJoin-like collaborative spend and a CoinJoin.

The recommended threshold is the smallest level whose FPR is within
:data:`TARGET_FPR` with a usable TPR; :data:`DEFAULT_MIN_SCORE` is that value
and a unit test reruns the calibration to keep them equal. The fixture's
addresses and hashes are fictional: labels are expanded into correctly
shaped addresses by :mod:`src.kb.onchain_identifiers`.

Stdlib + duckdb (imported lazily); no network.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.kb.onchain_identifiers import (
    BITCOIN,
    ETHEREUM,
    family,
    fixture_btc_address,
    fixture_evm_address,
    fixture_tx_hash,
)

FIXTURE = (
    Path(__file__).resolve().parents[2] / "config/onchain/cluster-calibration.json"
)
REPORT = FIXTURE.with_name("cluster-calibration-report.json")
TARGET_FPR = 0.1
MIN_TPR = 0.5
DEFAULT_LEVELS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0)
# The served default: the smallest calibrated level within the FPR target (rerun as a test).
DEFAULT_MIN_SCORE = 0.3
_NAMESPACE = "calibration"
_SCOPES = {"knowledge:onchain:write"}


def _address(chain: str, label: str) -> str:
    return (
        fixture_evm_address(label).lower()
        if family(chain) == "evm"
        else fixture_btc_address(label)
    )


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def build_ledger(scenario: Mapping[str, Any]):
    """An in-memory store holding one scenario's records, as acquisition would have written them."""
    import duckdb

    from src.kb.onchain import OnchainStore

    chain = {"ethereum": ETHEREUM, "bitcoin": BITCOIN}[scenario["chain"]]
    evm = family(chain) == "evm"
    conn = duckdb.connect(":memory:")
    store = OnchainStore(conn, now=lambda: 1)
    head = 1_000_000
    facts: dict[str, Any] = {
        "head_block": head,
        "transactions": [],
        "deployments": [],
        "funding": [],
    }
    for tx in scenario.get("transactions") or []:
        tx_hash = fixture_tx_hash(f"{scenario['name']}:{tx['id']}", evm=evm)
        block = int(tx["block"])
        if evm:
            facts["transactions"].append(
                {
                    "tx_hash": tx_hash,
                    "block_number": block,
                    "timestamp": None,
                    "from": _address(chain, tx["from"]),
                    "to": _address(chain, tx["to"]),
                    "value": str(tx.get("value", 1)),
                    "status": "success",
                    "confirmations": head - block + 1,
                }
            )
        else:
            facts["transactions"].append(
                {
                    "tx_hash": tx_hash,
                    "block_number": block,
                    "timestamp": None,
                    "status": "success",
                    "confirmations": head - block + 1,
                    "inputs": [
                        {
                            "address": _address(chain, a),
                            "value": 1000,
                            "prev_tx": None,
                            "prev_index": 0,
                        }
                        for a in tx["inputs"]
                    ],
                    "outputs": [
                        {"address": _address(chain, a), "value": v, "index": n}
                        for n, (a, v) in enumerate(tx["outputs"])
                    ],
                }
            )
    for dep in scenario.get("deployments") or []:
        facts["transactions"].append(
            {
                "tx_hash": fixture_tx_hash(
                    f"{scenario['name']}:{dep['contract']}:deploy"
                ),
                "block_number": int(dep["block"]),
                "from": _address(chain, dep["deployer"]),
                "to": None,
                "value": "0",
                "status": "success",
                "confirmations": head,
                "contract_created": _address(chain, dep["contract"]),
            }
        )
        facts["deployments"].append(
            {
                "contract_address": _address(chain, dep["contract"]),
                "deployer": _address(chain, dep["deployer"]),
                "tx_hash": fixture_tx_hash(
                    f"{scenario['name']}:{dep['contract']}:deploy"
                ),
                "block_number": int(dep["block"]),
            }
        )
    for fund in scenario.get("funding") or []:
        facts["transactions"].append(
            {
                "tx_hash": fixture_tx_hash(
                    f"{scenario['name']}:{fund['address']}:fund"
                ),
                "block_number": int(fund["block"]),
                "from": _address(chain, fund["from"]),
                "to": _address(chain, fund["address"]),
                "value": "1",
                "status": "success",
                "confirmations": head,
            }
        )
        facts["funding"].append(
            {
                "address": _address(chain, fund["address"]),
                "complete": True,
                "window": {"from_block": 0},
                "first_inbound": [
                    {
                        "tx_hash": fixture_tx_hash(
                            f"{scenario['name']}:{fund['address']}:fund"
                        ),
                        "from": _address(chain, fund["from"]),
                        "value": "1",
                        "block_number": int(fund["block"]),
                    }
                ],
            }
        )
    observation = {
        "observation_id": f"onchain-obs:calibration-{scenario['name']}",
        "provider": "calibration",
        "operation": "fixture",
        "chain_id": chain,
        "subject": {"kind": "fixture", "value": scenario["name"]},
        "observed_at_ms": 1,
        "request_id": f"calibration:{scenario['name']}",
        "facts": facts,
    }
    store.record_observation(_NAMESPACE, observation)
    store.project(_NAMESPACE, observation, principal_id="calibration", scopes=_SCOPES)
    return conn, chain


def _clustered(conn: Any, chain: str, a: str, b: str, level: float) -> bool:
    from src.kb.onchain_clustering import address_cluster

    result = address_cluster(
        conn,
        _NAMESPACE,
        chain,
        a,
        min_score=level,
        max_depth=3,
        max_edges=200,
        report_calibration=False,
    )
    return any(m["address"] == b for m in result.get("members") or [])


def calibrate(
    scenarios: Sequence[Mapping[str, Any]] | None = None,
    levels: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Pair-level FPR/TPR per ``min_score`` level and the recommended (smallest within-target) level."""
    scenarios = list(
        scenarios if scenarios is not None else load_fixture()["scenarios"]
    )
    levels = sorted(levels or DEFAULT_LEVELS)
    built = []
    for scenario in scenarios:
        conn, chain = build_ledger(scenario)
        built.append((scenario, conn, chain))
    rows = []
    try:
        for level in levels:
            fp = tp = pos = neg = 0
            for scenario, conn, chain in built:
                for pair in scenario["pairs"]:
                    hit = _clustered(
                        conn,
                        chain,
                        _address(chain, pair["a"]),
                        _address(chain, pair["b"]),
                        level,
                    )
                    if pair["related"]:
                        pos += 1
                        tp += hit
                    else:
                        neg += 1
                        fp += hit
            rows.append(
                {
                    "min_score": level,
                    "fpr": round(fp / neg, 4) if neg else 0.0,
                    "tpr": round(tp / pos, 4) if pos else 1.0,
                    "false_positives": fp,
                    "true_positives": tp,
                    "related_pairs": pos,
                    "coincidental_pairs": neg,
                }
            )
    finally:
        for _, conn, _ in built:
            conn.close()
    recommended = next(
        (r for r in rows if r["fpr"] <= TARGET_FPR and r["tpr"] >= MIN_TPR), None
    )
    if recommended is None:
        recommended = min(rows, key=lambda r: (r["fpr"], -r["tpr"]))
    return {
        "target_fpr": TARGET_FPR,
        "min_tpr": MIN_TPR,
        "levels": rows,
        "recommended": recommended,
        "fixture": "config/onchain/cluster-calibration.json",
        "scenarios": len(scenarios),
    }


def render_report(result: Mapping[str, Any] | None = None) -> str:
    """The committed calibration report (``python -m src.kb.onchain_calibration`` rewrites it)."""
    result = dict(result or calibrate())
    return json.dumps(result, indent=1, sort_keys=True) + "\n"


@functools.lru_cache(maxsize=1)
def _served() -> dict[str, Any]:
    # The committed report, not a per-request rerun: a unit test reruns the calibration and
    # asserts it still equals this report, so the served numbers cannot drift from the fixture.
    result = json.loads(REPORT.read_text())
    at_default = next(
        (r for r in result["levels"] if r["min_score"] == DEFAULT_MIN_SCORE), None
    )
    return {
        "fixture": result["fixture"],
        "report": "config/onchain/cluster-calibration-report.json",
        "target_fpr": result["target_fpr"],
        "scenarios": result["scenarios"],
        "measured_at_default": at_default,
        "recommended_min_score": result["recommended"]["min_score"],
        "note": "false-positive rate measured on a labelled synthetic fixture; real-world rates differ",
    }


def served_calibration() -> dict[str, Any]:
    """The measured FPR/TPR at the served default, from the committed calibration report."""
    try:
        return dict(_served())
    except Exception as exc:  # noqa: BLE001 - a missing report must not break the query
        return {"status": "unavailable", "reason": type(exc).__name__}


if __name__ == "__main__":
    REPORT.write_text(render_report())
