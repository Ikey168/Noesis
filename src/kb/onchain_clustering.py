"""Probable address clustering over cited on-chain records (#2053, B04 #2057).

``address_cluster`` reports the addresses *probably* controlled together
with a queried one, framed like the OSINT reporting-origin graph: every edge
names its heuristic and cites the transactions that establish it, the result
carries a caveat, a null model and the calibrated threshold, and there is no
owner or attribution field anywhere.

Heuristics are separate, declared edge types (:data:`HEURISTICS`):

* ``common-input`` (UTXO chains): addresses spent together as inputs of one
  transaction. Transactions that look like CoinJoins (at least three inputs
  and at least three outputs of one identical value) are excluded and listed.
* ``deposit-address-reuse`` (EVM chains): senders that pay the same
  deposit-like address, where that address sweeps what it receives to a
  single other address. A hot wallet or bridge receiving from many unrelated
  users is the known false-positive case; the hub-size score below is what
  keeps it out.
* ``deployer-funding`` (EVM chains): a contract deployer and the address
  that sent its first inbound value. Exchanges and bridges fund many
  unrelated accounts; again the hub-size score is the guard.

Every edge has a score ``1 / (hub size - 1)`` (common-input, deposit reuse:
the hub is the transaction or deposit address and its size the number of
distinct addresses sharing it) or ``1 / fan-out`` (deployer funding: the
number of first-funded addresses sharing the funder). An edge counts when its
score reaches ``min_score``; the served default is the calibrated value in
:mod:`src.kb.onchain_calibration`.

Stdlib-only over the B02 records with an injected read-only connection,
mirroring :mod:`src.osint.independence`. Traversal depth and edge count are
bounded. The query refuses to run without at least one cited transaction for
the address.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.onchain import (
    NOT_ATTRIBUTION,
    citation,
    digest,
    generation,
    labels_for,
    not_ready,
    ready,
)
from src.kb.onchain_identifiers import (
    IdentifierError,
    display_address,
    family,
    normalize_address,
    normalize_chain,
)

METHOD = "declared-heuristic address clustering over cited explorer records"
NOTE = (
    "Clustering is a probabilistic heuristic, not an identity finding. Exchange, bridge and mixer activity "
    "(and CoinJoin or PayJoin transactions) commonly break it. A cluster says which addresses are probably "
    "controlled together, never by whom; labels shown are quoted from their sources."
)
HEURISTICS: dict[str, dict[str, Any]] = {
    "common-input": {
        "chains": "utxo",
        "assumption": "inputs spent in one transaction are usually signed by one wallet",
        "score": "1 / (distinct input addresses - 1)",
        "breaks_when": [
            "CoinJoin and PayJoin transactions",
            "custodial batching by exchanges",
        ],
        "excluded": "transactions with >= 3 inputs and >= 3 outputs of one identical value (CoinJoin-like)",
    },
    "deposit-address-reuse": {
        "chains": "evm",
        "assumption": "a deposit address that sweeps to a single address is issued to one customer, so the "
        "addresses paying it are probably controlled together",
        "score": "1 / (distinct senders to the deposit address - 1)",
        "breaks_when": [
            "hot wallets and bridge contracts that receive from many unrelated users",
            "shared deposit addresses of payment processors",
        ],
        "requires": "the deposit address's own outbound transactions must have been acquired",
    },
    "deployer-funding": {
        "chains": "evm",
        "assumption": "a contract deployer is often funded by another address of the same operator",
        "score": "1 / (addresses whose first inbound value came from the same funder)",
        "breaks_when": [
            "exchange withdrawals and bridges that fund many unrelated accounts"
        ],
    },
}
MAX_DEPTH = 3
MAX_EDGES = 200
NULL_PERMUTATIONS = 200


def _load(row: Any) -> dict[str, Any] | None:
    try:
        return json.loads(row)
    except (TypeError, ValueError):
        return None


class _Ledger:
    """Read-only access to current records for one namespace and chain, cached per query."""

    def __init__(self, conn: Any, namespace: str, chain: str) -> None:
        self.conn, self.namespace, self.chain = conn, namespace, chain
        self.cache: dict[str, Any] = {}
        self._funding: dict[str, list[str]] | None = None

    def records(
        self, address: str, kinds: Iterable[str], roles: Iterable[str]
    ) -> list[tuple[str, str, dict]]:
        kinds, roles = list(kinds), list(roles)
        rows = self.conn.execute(
            "SELECT DISTINCT c.kind, c.observation_id, c.content_json FROM onchain_address_roles r JOIN "
            "onchain_current c USING(namespace, kind, record_key) WHERE r.namespace=? AND r.chain_id=? AND "
            f"r.address=? AND r.kind IN ({','.join('?' for _ in kinds)}) AND r.role IN "
            f"({','.join('?' for _ in roles)}) ORDER BY c.observation_id",
            [self.namespace, self.chain, address, *kinds, *roles],
        ).fetchall()
        out = []
        for kind, observation_id, content in rows:
            loaded = _load(content)
            if loaded is not None and loaded.get("state") != "dropped":
                out.append((kind, observation_id, loaded))
        return out

    def has_cited_transaction(self, address: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM onchain_address_roles WHERE namespace=? AND chain_id=? AND address=? AND kind IN "
                "('transaction','transfer') LIMIT 1",
                [self.namespace, self.chain, address],
            ).fetchone()
        )

    def cite(self, observation_id: str, tx_hash: str | None) -> dict[str, Any]:
        return {
            "tx_hash": tx_hash,
            "citation": citation(
                self.conn,
                self.chain,
                observation_id,
                kind="tx",
                identifier=tx_hash,
                cache=self.cache,
            ),
        }

    def flows(self, address: str, role: str) -> list[dict[str, Any]]:
        """Successful value flows (native transactions and token transfers) where ``address`` has ``role``."""
        out = []
        for kind, observation_id, content in self.records(
            address, ("transaction", "transfer"), (role,)
        ):
            if (
                content.get("status") == "failed"
                or not content.get("from")
                or not content.get("to")
            ):
                continue
            if int(content.get("value") or 0) <= 0:
                continue
            out.append(
                {
                    "from": content["from"],
                    "to": content["to"],
                    "tx_hash": content["tx_hash"],
                    "block_number": content.get("block_number"),
                    "observation_id": observation_id,
                    "kind": kind,
                }
            )
        return out

    def first_funders(self) -> dict[str, list[str]]:
        """funder -> addresses whose first inbound value it sent (one definition for queries and monitors)."""
        if self._funding is None:
            table: dict[str, set[str]] = {}
            for (content,) in self.conn.execute(
                "SELECT content_json FROM onchain_current WHERE namespace=? AND chain_id=? AND kind='funding'",
                [self.namespace, self.chain],
            ).fetchall():
                loaded = _load(content)
                first = (loaded or {}).get("first_inbound") or []
                if first and first[0].get("from"):
                    table.setdefault(first[0]["from"], set()).add(loaded["address"])
            self._funding = {k: sorted(v) for k, v in table.items()}
        return self._funding

    def funding(self, address: str) -> tuple[str, dict[str, Any]] | None:
        row = self.conn.execute(
            "SELECT observation_id, content_json FROM onchain_current WHERE namespace=? AND "
            "kind='funding' AND record_key=?",
            [self.namespace, f"{self.chain}|{address}"],
        ).fetchone()
        if row is None:
            return None
        loaded = _load(row[1])
        return (row[0], loaded) if loaded else None

    def is_deployer(self, address: str) -> list[dict[str, Any]]:
        return [
            c
            for _, _, c in self.records(
                address, ("contract_deployment",), ("deployer",)
            )
        ]


def coinjoin_like(content: Mapping[str, Any]) -> bool:
    inputs = {i.get("address") for i in content.get("inputs") or [] if i.get("address")}
    values: dict[str, int] = {}
    for output in content.get("outputs") or []:
        values[str(output.get("value"))] = values.get(str(output.get("value")), 0) + 1
    return len(inputs) >= 3 and max(values.values(), default=0) >= 3


def candidate_edges(
    ledger: _Ledger, address: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every heuristic edge touching ``address`` (before the threshold), and what was excluded or unknown."""
    edges: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    if family(ledger.chain) == "utxo":
        for _, observation_id, tx in ledger.records(
            address, ("transaction",), ("input",)
        ):
            inputs = sorted(
                {
                    i["address"]
                    for i in tx.get("inputs") or []
                    if i.get("address") and not i.get("coinbase")
                }
            )
            if len(inputs) < 2:
                continue
            if coinjoin_like(tx):
                excluded.append(
                    {
                        "heuristic": "common-input",
                        "tx_hash": tx["tx_hash"],
                        "reason": "coinjoin_like",
                        "inputs": len(inputs),
                    }
                )
                continue
            score = 1.0 / (len(inputs) - 1)
            for other in inputs:
                if other != address:
                    edges.append(
                        {
                            "heuristic": "common-input",
                            "other": other,
                            "score": score,
                            "hub": {
                                "kind": "transaction",
                                "id": tx["tx_hash"],
                                "size": len(inputs),
                            },
                            "evidence": [ledger.cite(observation_id, tx["tx_hash"])],
                        }
                    )
        return edges, excluded
    # deposit-address reuse: address -> D, D sweeps to one H
    for flow in ledger.flows(address, "from"):
        deposit = flow["to"]
        if deposit == address:
            continue
        sweeps = ledger.flows(deposit, "from")
        if not sweeps:
            excluded.append(
                {
                    "heuristic": "deposit-address-reuse",
                    "address": deposit,
                    "reason": "outbound_not_acquired",
                }
            )
            continue
        targets = {s["to"] for s in sweeps}
        if len(targets) != 1 or deposit in targets:
            continue
        inbound = [
            f
            for f in ledger.flows(deposit, "to")
            if f["from"] not in targets | {deposit}
        ]
        first_in = min(
            (f["block_number"] for f in inbound if f["block_number"] is not None),
            default=None,
        )
        if first_in is None or not any(
            (s["block_number"] or 0) >= first_in for s in sweeps
        ):
            continue
        senders = sorted({f["from"] for f in inbound})
        if len(senders) < 2:
            continue
        score = 1.0 / (len(senders) - 1)
        sweep_evidence = [
            ledger.cite(s["observation_id"], s["tx_hash"]) for s in sweeps[:3]
        ]
        for other in senders:
            if other == address:
                continue
            paid = [f for f in inbound if f["from"] in {address, other}][:4]
            edges.append(
                {
                    "heuristic": "deposit-address-reuse",
                    "other": other,
                    "score": score,
                    "hub": {
                        "kind": "deposit-address",
                        "id": deposit,
                        "size": len(senders),
                        "sweeps_to": next(iter(targets)),
                    },
                    "evidence": [
                        ledger.cite(f["observation_id"], f["tx_hash"]) for f in paid
                    ]
                    + sweep_evidence,
                }
            )
    # deployer funding: deployer <-> first funder
    funders = ledger.first_funders()
    pairs: list[tuple[str, str]] = []
    if ledger.is_deployer(address):
        found = ledger.funding(address)
        first = (found or ("", {}))[1].get("first_inbound") or []
        if first and first[0].get("from"):
            pairs.append((address, first[0]["from"]))
    for funded in funders.get(address, []):
        if ledger.is_deployer(funded):
            pairs.append((funded, address))
    for deployer, funder in pairs:
        found = ledger.funding(deployer)
        if found is None:
            continue
        observation_id, content = found
        first = content["first_inbound"][0]
        fan_out = len(funders.get(funder, [])) or 1
        edges.append(
            {
                "heuristic": "deployer-funding",
                "other": funder if deployer == address else deployer,
                "score": 1.0 / fan_out,
                "hub": {"kind": "funder", "id": funder, "size": fan_out},
                "evidence": [ledger.cite(observation_id, first["tx_hash"])],
            }
        )
    return edges, excluded


def _null_model(
    hubs: Mapping[str, set[str]], address: str, min_score: float, observed: int
) -> dict[str, Any]:
    """Degree-preserving reassignment of addresses to the discovered hubs.

    Hub sizes and each address's number of hub memberships are kept; which
    addresses share a hub is shuffled. The expected number of depth-1
    neighbours of the queried address under that shuffle is how many links
    chance alone would produce with this local structure.
    """
    slots = [
        (hub, member)
        for hub, members in sorted(hubs.items())
        for member in sorted(members)
    ]
    if len(hubs) < 2 or not any(m == address for _, m in slots):
        return {
            "method": "degree-preserving hub shuffle",
            "permutations": 0,
            "expected_neighbours": None,
            "observed_neighbours": observed,
            "note": "fewer than two hubs: the null model is uninformative here",
        }
    rng = random.Random(int(digest([address, sorted(hubs)])[:12], 16))
    members = [m for _, m in slots]
    total = 0
    for _ in range(NULL_PERMUTATIONS):
        rng.shuffle(members)
        assigned: dict[str, set[str]] = {}
        for (hub, _), member in zip(slots, members, strict=True):
            assigned.setdefault(hub, set()).add(member)
        neighbours = set()
        for group in assigned.values():
            if (
                address in group
                and len(group) >= 2
                and 1.0 / (len(group) - 1) >= min_score
            ):
                neighbours |= group - {address}
        total += len(neighbours)
    expected = total / NULL_PERMUTATIONS
    return {
        "method": "degree-preserving hub shuffle",
        "permutations": NULL_PERMUTATIONS,
        "expected_neighbours": round(expected, 4),
        "observed_neighbours": observed,
        "excess_over_null": round(observed - expected, 4),
        "note": "an observed count close to the null expectation means the links add little beyond chance",
    }


def address_cluster(
    conn: Any,
    namespace: str,
    chain_id: str,
    address: str,
    *,
    min_score: float | None = None,
    max_depth: int = 2,
    max_edges: int = 50,
    report_calibration: bool = True,
) -> dict[str, Any]:
    """Addresses probably controlled together with ``address``, each edge with its heuristic and citations."""
    from src.kb.onchain_calibration import DEFAULT_MIN_SCORE, served_calibration

    if not ready(conn):
        return not_ready(address=address, chain_id=chain_id)
    try:
        chain = normalize_chain(chain_id)
        start = normalize_address(chain, address)
    except IdentifierError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}
    threshold = DEFAULT_MIN_SCORE if min_score is None else float(min_score)
    if not 0.0 < threshold <= 1.0:
        return {
            "status": "error",
            "code": "invalid_threshold",
            "error": "min_score must be in (0, 1]",
        }
    max_depth = max(1, min(int(max_depth or 2), MAX_DEPTH))
    max_edges = max(1, min(int(max_edges or 50), MAX_EDGES))
    ledger = _Ledger(conn, namespace, chain)
    base = {
        "namespace": namespace,
        "chain_id": chain,
        "address": start,
        "display_address": display_address(chain, start),
        "note": NOTE,
        "attribution": NOT_ATTRIBUTION,
        "heuristics": HEURISTICS,
        "generation": generation(conn, namespace),
    }
    if not ledger.has_cited_transaction(start):
        return {
            **base,
            "status": "no_cited_transaction",
            "members": [],
            "edges": [],
            "reason": "clustering refuses to run without at least one cited transaction for the address",
            "n": 0,
            "method": METHOD,
            "assumptions": [NOTE],
        }
    depth = {start: 0}
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    hubs: dict[str, set[str]] = {}
    frontier, truncated = [start], False
    while frontier and not truncated:
        nxt = []
        for node in frontier:
            if depth[node] >= max_depth:
                continue
            found, skipped = candidate_edges(ledger, node)
            excluded += [e for e in skipped if e not in excluded]
            for edge in sorted(
                found, key=lambda e: (-e["score"], e["heuristic"], e["other"])
            ):
                a, b = sorted((node, edge["other"]))
                key = (a, b, edge["heuristic"], edge["hub"]["id"])
                hubs.setdefault(
                    f"{edge['heuristic']}:{edge['hub']['id']}", set()
                ).update({node, edge["other"]})
                if edge["score"] < threshold:
                    if node == start:
                        below.append(
                            {
                                "heuristic": edge["heuristic"],
                                "address": edge["other"],
                                "score": round(edge["score"], 4),
                                "hub": edge["hub"],
                            }
                        )
                    continue
                if key in edges:
                    continue
                if len(edges) >= max_edges:
                    truncated = True
                    break
                edges[key] = {
                    "a": a,
                    "b": b,
                    "heuristic": edge["heuristic"],
                    "score": round(edge["score"], 4),
                    "hub": edge["hub"],
                    "status": "probable",
                    "evidence": edge["evidence"],
                }
                if edge["other"] not in depth:
                    depth[edge["other"]] = depth[node] + 1
                    nxt.append(edge["other"])
            if truncated:
                break
        frontier = nxt
    members = [
        {
            "address": a,
            "display_address": display_address(chain, a),
            "depth": d,
            "via": sorted(
                {e["heuristic"] for e in edges.values() if a in (e["a"], e["b"])}
            ),
        }
        for a, d in sorted(depth.items(), key=lambda item: (item[1], item[0]))
        if a != start
    ]
    neighbours = {
        e["b"] if e["a"] == start else e["a"]
        for e in edges.values()
        if start in (e["a"], e["b"])
    }
    calibration = (
        served_calibration() if report_calibration else {"status": "not_reported"}
    )
    return {
        **base,
        "status": "probable" if edges else "no_probable_cluster",
        "members": members,
        "edges": sorted(edges.values(), key=lambda e: (e["a"], e["b"], e["heuristic"])),
        "below_threshold": below[:25],
        "excluded": excluded[:50],
        "truncated": truncated,
        "bounds": {"max_depth": max_depth, "max_edges": max_edges},
        "threshold": {
            "min_score": threshold,
            "served_default": DEFAULT_MIN_SCORE,
            "calibration": calibration,
        },
        "null_model": _null_model(hubs, start, threshold, len(neighbours)),
        "labels": labels_for(
            conn, namespace, chain, [start, *(m["address"] for m in members)]
        ),
        "n": len(edges),
        "method": METHOD,
        "assumptions": [h["assumption"] for h in HEURISTICS.values()] + [NOTE],
    }
