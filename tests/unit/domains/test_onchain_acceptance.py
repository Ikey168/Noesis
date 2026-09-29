"""Offline acceptance journey of the On-chain Observations pack (#2053, B05 #2058).

A contract address reaches a cited deployer and first-funding chain; an
address reaches its cited transfers and a probable cluster with the heuristic,
caveat and measured false-positive rate; an unknown address is an explicit
unknown. Everything replays the pinned fixtures with sockets blocked. Live
evidence is reported separately (``LIVE_VERIFICATION``) and is unverified.
"""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.ingestion import onchain as acq
from src.kb import onchain
from src.kb.onchain_calibration import DEFAULT_MIN_SCORE, TARGET_FPR
from src.kb.onchain_clustering import address_cluster
from src.osint.provenance import trace_artifact
from tests.unit.onchain import fixture_builder as fb

NS = fb.NAMESPACE


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_contract_and_address_to_cited_origin_transfers_and_probable_cluster():
    conn = duckdb.connect(":memory:")
    try:
        receipts = {source: fb.run_journey(conn, source) for source in fb.RAW_FILES}
        assert all(
            r["status"] in {"acquired", "not_labelled_by_source"}
            for rs in receipts.values()
            for r in rs
        )

        # Contract -> cited deployer and funding chain, unknown hop explicit.
        origin = onchain.contract_origin(
            conn, NS, "eip155:1", fb.ETH["exampla-token-contract"]
        )
        assert origin["status"] == "observed"
        assert origin["deployment"]["deployer"] == fb.lower("exampla-deployer")
        assert origin["deployment"]["tx_hash"] == fb.TX["deploy"]
        assert (
            origin["deployment"]["citation"]["explorer_url"]
            == f"https://etherscan.io/tx/{fb.TX['deploy']}"
        )
        assert origin["contract_funding"]["first_inbound"][0]["from"] == fb.lower(
            "first-buyer"
        )
        chain = origin["deployer_funding_chain"]
        assert [h["status"] for h in chain] == ["funded", "funded", "not_acquired"]
        assert [h["first_inbound"][0]["from"] for h in chain[:2]] == [
            fb.lower("exampla-treasury"),
            fb.lower("upstream-funder"),
        ]
        assert all(
            i["citation"]["cited"] for h in chain[:2] for i in h["first_inbound"]
        )
        assert {
            "kind": "funding_hop",
            "address": fb.lower("upstream-funder"),
            "status": "not_acquired",
        } in origin["unknowns"]
        label = origin["labels"][fb.lower("exampla-token-contract")][0]
        assert label["quote"] == "Exampla Token" and label["stated_by"].startswith(
            "MyEtherWallet"
        )

        # Address -> cited transfers in and out with coverage and unknowns.
        seen = onchain.address_observations(
            conn,
            NS,
            "eip155:1",
            fb.ETH["journey-user"],
            from_block=18_000_000,
            to_block=18_200_000,
        )
        assert seen["status"] == "observed"
        directions = {(i["kind"], i["tx_hash"]): i["direction"] for i in seen["items"]}
        assert directions[("transaction", fb.TX["sender-to-user"])] == "in"
        assert directions[("transaction", fb.TX["user-to-deposit"])] == "out"
        assert directions[("transfer", fb.TX["token-to-user"])] == "in"
        assert all(
            i["citation"]["cited"] and i["block_number"] and i["timestamp"]
            for i in seen["items"]
        )
        assert any(u["kind"] == "internal_transfers" for u in seen["unknowns"])
        counter = onchain.counterparties(
            conn, NS, "eip155:1", fb.ETH["journey-user"], as_of_block=18_060_000
        )
        assert [c["address"] for c in counter["counterparties"]] == [
            fb.lower("journey-sender")
        ]

        # ... and a probable cluster with heuristic, caveat, null model and measured FPR.
        cluster = address_cluster(conn, NS, "eip155:1", fb.ETH["journey-user"])
        assert cluster["status"] == "probable"
        assert {e["heuristic"] for e in cluster["edges"]} == {"deposit-address-reuse"}
        assert "probabilistic heuristic" in cluster["note"]
        measured = cluster["threshold"]["calibration"]["measured_at_default"]
        assert (
            cluster["threshold"]["min_score"] == DEFAULT_MIN_SCORE
            and measured["fpr"] <= TARGET_FPR
        )
        assert cluster["null_model"]["method"] == "degree-preserving hub shuffle"
        btc = address_cluster(conn, NS, "bitcoin", fb.BTC["wallet-one"])
        assert {e["heuristic"] for e in btc["edges"]} == {"common-input"}

        # Unknown address: an explicit unknown, not an empty success.
        unknown = onchain.address_observations(
            conn, NS, "eip155:1", fb.ETH["upstream-funder"]
        )
        assert unknown["status"] == "unknown"
        assert address_cluster(conn, NS, "eip155:1", fb.ETH["exampla-token-contract"])[
            "status"
        ] in {"no_cited_transaction", "no_probable_cluster"}

        # Provenance: every observation traces source -> receipt -> observation -> revisions.
        trace = trace_artifact(
            conn, document_id=receipts[acq.ETHERSCAN][2]["observation_id"]
        )
        assert trace["cited"] and trace["chain"][-1]["record_revisions"]

        # Re-running the whole journey with new request ids adds nothing.
        before = conn.execute("SELECT COUNT(*) FROM onchain_revisions").fetchone()[0]
        for source in fb.RAW_FILES:
            transport = acq.fixture_transport(fb.raw_documents()[source]["responses"])
            for n, replay in enumerate(fb.replays()[source]):
                call = getattr(acq, replay["call"])
                kwargs = {
                    "request_id": f"rerun:{source}:{n}",
                    "principal_id": "p",
                    "scopes": fb.SCOPES,
                    "transport": transport,
                    "now": lambda: fb.NOW_MS + 10_000,
                }
                if source == acq.ETHERSCAN:
                    kwargs["api_key"] = "fixture-secret-not-a-real-key"
                if replay["call"] in {"acquire_contract_origin", "acquire_label"}:
                    call(
                        conn,
                        NS,
                        replay["identifier"],
                        chain_id=replay["chain_id"],
                        **kwargs,
                    )
                else:
                    call(conn, NS, replay["chain_id"], replay["identifier"], **kwargs)
        assert (
            conn.execute("SELECT COUNT(*) FROM onchain_revisions").fetchone()[0]
            == before
        )
    finally:
        conn.close()


def test_live_evidence_is_reported_separately_and_unverified():
    assert set(acq.LIVE_VERIFICATION) == set(fb.RAW_FILES)
    assert {v["status"] for v in acq.LIVE_VERIFICATION.values()} == {"unverified-live"}
