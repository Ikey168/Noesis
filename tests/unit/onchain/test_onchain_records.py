"""On-chain observation records: revisions, keys, labels, references, readiness (#2055)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb import onchain
from src.kb.onchain import OnchainError, OnchainStore
from src.kb.onchain_identifiers import (
    BITCOIN,
    ETHEREUM,
    fixture_btc_address,
    fixture_evm_address,
    fixture_tx_hash,
)

ROOT = Path(__file__).resolve().parents[3]
NS = "records-test"
WRITE = {onchain.WRITE_SCOPE}
REVIEW = {onchain.WRITE_SCOPE, onchain.REVIEW_SCOPE}
A = fixture_evm_address("records-a").lower()
B = fixture_evm_address("records-b").lower()
TX = fixture_tx_hash("records-tx")


@pytest.fixture()
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


def observe(
    store,
    facts,
    *,
    at=1,
    oid=None,
    chain=ETHEREUM,
    provider="etherscan-v2-mainnet",
    license_=None,
):
    record = {
        "observation_id": oid or f"onchain-obs:{at:028d}",
        "provider": provider,
        "operation": "test",
        "chain_id": chain,
        "subject": {"kind": "test", "value": "x"},
        "observed_at_ms": at,
        "request_id": f"req-{at}",
        "facts": facts,
        "publisher": "Fixture",
        "license": license_ or {},
        "attribution": "Fixture attribution",
    }
    store.record_observation(NS, record)
    return store.project(NS, record, principal_id="tester", scopes=WRITE)


def tx(
    block=100, block_hash="0x" + "a" * 64, confirmations=10, value="5", status="success"
):
    return {
        "tx_hash": TX,
        "block_number": block,
        "block_hash": block_hash,
        "timestamp": "2024-01-01T00:00:00Z",
        "from": A,
        "to": B,
        "value": value,
        "status": status,
        "confirmations": confirmations,
    }


def history(conn, key=f"{ETHEREUM}|{TX}"):
    return onchain.record_history(conn, NS, "transaction", key)["revisions"]


def test_reads_are_not_ready_before_any_acquisition(conn):
    assert onchain.address_observations(conn, NS, ETHEREUM, A)["status"] == "not_ready"
    assert onchain.contract_origin(conn, NS, ETHEREUM, A)["status"] == "not_ready"
    assert (
        onchain.counterparties(conn, NS, ETHEREUM, A, as_of_block=5)["status"]
        == "not_ready"
    )
    assert onchain.record_history(conn, NS, "transaction", "x")["status"] == "not_ready"
    assert onchain.readiness(conn, NS)["status"] == "not_ready"
    assert onchain.trace_observation(conn, "onchain-obs:x")["code"] == "not_ready"
    assert onchain.revision_as_of(conn, NS, "transaction", "x", as_of_block=1) is None


def test_unchanged_reacquisition_adds_nothing_and_confirmations_are_not_content(conn):
    store = OnchainStore(conn)
    first = observe(
        store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1
    )
    assert [c["change"] for c in first["changes"]] == ["initial"]
    # More confirmations while still below the finality depth: nothing new.
    again = observe(
        store, {"head_block": 130, "transactions": [tx(confirmations=31)]}, at=2
    )
    assert again["added"] == 0 and len(history(conn)) == 1


def test_finality_crossing_is_an_explicit_revision(conn):
    store = OnchainStore(conn)
    observe(store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1)
    result = observe(
        store, {"head_block": 200, "transactions": [tx(confirmations=101)]}, at=2
    )
    assert [c["change"] for c in result["changes"]] == ["finality"]
    assert [r["content"]["finality"] for r in history(conn)] == ["unconfirmed", "final"]


def test_reorg_is_modelled_and_late_older_data_is_history_without_a_correction(conn):
    store = OnchainStore(conn)
    observe(store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1)
    moved = observe(
        store,
        {
            "head_block": 120,
            "transactions": [
                tx(block=105, block_hash="0x" + "b" * 64, confirmations=16)
            ],
        },
        at=2,
    )
    assert [c["change"] for c in moved["changes"]] == ["reorg"]
    # A late read from an older head reporting yet another block: kept as history, not current, no correction.
    late = observe(
        store,
        {
            "head_block": 108,
            "transactions": [
                tx(block=101, block_hash="0x" + "c" * 64, confirmations=8)
            ],
        },
        at=3,
    )
    assert [c["change"] for c in late["changes"]] == ["history"] and late["changes"][0][
        "current"
    ] is False
    revisions = history(conn)
    assert [r["change"] for r in revisions] == ["initial", "reorg", "history"]
    current = onchain.address_observations(conn, NS, ETHEREUM, A)
    # no coverage window recorded here, so the address stays unknown even though records exist
    assert current["status"] == "unknown"
    # Late older data equal to a known revision adds nothing at all.
    assert (
        observe(
            store,
            {
                "head_block": 108,
                "transactions": [
                    tx(block=101, block_hash="0x" + "c" * 64, confirmations=8)
                ],
            },
            at=4,
        )["added"]
        == 0
    )


def test_reversion_to_earlier_content_is_a_new_revision(conn):
    store = OnchainStore(conn)
    observe(store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1)
    observe(
        store,
        {"head_block": 120, "transactions": [tx(confirmations=21, status="failed")]},
        at=2,
    )
    back = observe(
        store, {"head_block": 130, "transactions": [tx(confirmations=31)]}, at=3
    )
    assert [c["change"] for c in back["changes"]] == ["reversion"]
    assert [r["revision"] for r in history(conn)] == [1, 2, 3]


def test_dropped_transaction_is_explicit_but_a_final_one_never_drops(conn):
    store = OnchainStore(conn)
    observe(store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1)
    dropped = observe(store, {"head_block": 111, "dropped": [TX]}, at=2)
    assert [c["change"] for c in dropped["changes"]] == ["dropped"]
    assert history(conn)[-1]["content"]["state"] == "dropped"
    other = fixture_tx_hash("records-final")
    observe(
        store,
        {
            "head_block": 500,
            "transactions": [{**tx(confirmations=401), "tx_hash": other}],
        },
        at=3,
    )
    assert observe(store, {"head_block": 501, "dropped": [other]}, at=4)["added"] == 0


def test_revision_as_of_uses_the_revision_current_at_that_head(conn):
    store = OnchainStore(conn)
    observe(store, {"head_block": 110, "transactions": [tx(confirmations=11)]}, at=1)
    observe(
        store,
        {
            "head_block": 120,
            "transactions": [
                tx(block=105, block_hash="0x" + "b" * 64, confirmations=16)
            ],
        },
        at=2,
    )
    key = f"{ETHEREUM}|{TX}"
    assert (
        onchain.revision_as_of(conn, NS, "transaction", key, as_of_block=115)[
            "revision"
        ]
        == 1
    )
    assert (
        onchain.revision_as_of(conn, NS, "transaction", key, as_of_block=125)[
            "revision"
        ]
        == 2
    )
    assert onchain.revision_as_of(conn, NS, "transaction", key, as_of_block=100) is None


def test_same_address_string_on_two_chains_is_two_records(conn):
    store = OnchainStore(conn)
    body = "ab" * 32
    observe(
        store,
        {
            "head_block": 200,
            "transactions": [{**tx(confirmations=101), "tx_hash": "0x" + body}],
        },
        at=1,
    )
    btc_tx = {
        "tx_hash": body,
        "block_number": 50,
        "block_hash": "cd" * 32,
        "timestamp": None,
        "status": "success",
        "confirmations": 10,
        "inputs": [{"address": fixture_btc_address("r1"), "value": 5}],
        "outputs": [{"address": fixture_btc_address("r2"), "value": 4, "index": 0}],
    }
    observe(store, {"head_block": 59, "transactions": [btc_tx]}, at=2, chain=BITCOIN)
    keys = {
        r[0]
        for r in conn.execute(
            "SELECT record_key FROM onchain_current WHERE kind='transaction'"
        ).fetchall()
    }
    assert keys == {f"{ETHEREUM}|0x{body}", f"{BITCOIN}|{body}"}


def test_missing_values_stay_absent_never_the_text_none(conn):
    store = OnchainStore(conn)
    observe(
        store,
        {
            "head_block": None,
            "transactions": [
                {
                    "tx_hash": TX,
                    "block_number": None,
                    "from": A,
                    "to": None,
                    "value": None,
                    "status": None,
                }
            ],
        },
        at=1,
    )
    content = history(conn)[0]["content"]
    assert (
        content["to"] is None
        and content["value"] is None
        and content["finality"] == "pending"
    )
    assert "None" not in json.dumps(content)


def test_records_validate_against_the_registered_schema_and_carry_no_owner(conn):
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-onchain-observation-v1.json"
        ).read_text()
    )
    Draft7Validator.check_schema(schema)
    store = OnchainStore(conn)
    contract = fixture_evm_address("records-contract").lower()
    observe(
        store,
        {
            "head_block": 300,
            "transactions": [tx(confirmations=201)],
            "transfers": [
                {
                    "tx_hash": TX,
                    "token_contract": contract,
                    "token_symbol": "EXA",
                    "from": A,
                    "to": B,
                    "value": "10",
                    "block_number": 100,
                    "log_index": 2,
                    "confirmations": 201,
                }
            ],
            "deployments": [
                {
                    "contract_address": contract,
                    "deployer": A,
                    "tx_hash": TX,
                    "block_number": 99,
                }
            ],
            "funding": [
                {
                    "address": A,
                    "complete": True,
                    "window": {"from_block": 0, "to_block": 300},
                    "first_inbound": [
                        {"tx_hash": TX, "from": B, "value": "7", "block_number": 90}
                    ],
                }
            ],
            "labels": [
                {
                    "address": contract,
                    "label": "Exampla Token",
                    "category": "ERC20",
                    "attributes": {"symbol": "EXA"},
                    "label_id": "token",
                }
            ],
        },
        at=1,
        license_={"id": "MIT"},
    )
    rows = conn.execute(
        "SELECT kind, record_key, revision, revision_id, chain_id, as_of_block, observation_id, change, "
        "content_json FROM onchain_revisions"
    ).fetchall()
    assert {r[0] for r in rows} == set(onchain.KINDS)
    validator = Draft7Validator(schema)
    for kind, key, rev, rid, chain, as_of, oid, change, content in rows:
        doc = {
            "kind": kind,
            "record_key": key,
            "revision": rev,
            "revision_id": rid,
            "chain_id": chain,
            "as_of_block": as_of,
            "observation_id": oid,
            "change": change,
            "content": json.loads(content),
        }
        assert not list(validator.iter_errors(doc)), (
            kind,
            list(validator.iter_errors(doc))[:1],
        )
        assert not {"owner", "controller", "owned_by", "person", "attributed_to"} & set(
            json.loads(content)
        )
    owner = {**json.loads(rows[0][8]), "owner": "someone"}
    bad = {
        "kind": rows[0][0],
        "record_key": "k",
        "revision": 1,
        "revision_id": "onchain-rev:" + "0" * 28,
        "chain_id": ETHEREUM,
        "observation_id": "onchain-obs:x",
        "change": "initial",
        "content": owner,
    }
    assert list(validator.iter_errors(bad))


def test_listing_survives_a_bad_row_and_generation_changes_with_any_record(conn):
    store = OnchainStore(conn)
    observe(
        store,
        {
            "head_block": 200,
            "transactions": [tx(confirmations=101)],
            "coverage": [
                {
                    "address": A,
                    "operation": "address-transactions",
                    "from_block": 0,
                    "to_block": 200,
                    "complete": True,
                }
            ],
        },
        at=1,
    )
    before = onchain.generation(conn, NS)
    other = fixture_tx_hash("records-other")
    observe(
        store,
        {
            "head_block": 201,
            "transactions": [{**tx(confirmations=102), "tx_hash": other}],
        },
        at=2,
    )
    assert onchain.generation(conn, NS) != before
    conn.execute(
        "UPDATE onchain_current SET content_json='{broken' WHERE record_key=?",
        [f"{ETHEREUM}|{other}"],
    )
    result = onchain.address_observations(conn, NS, ETHEREUM, A)
    assert (
        result["status"] == "observed"
        and result["skipped_rows"] == 1
        and result["count"] == 1
    )


def test_unknown_address_is_explicit_and_covered_silence_is_not_empty_success(conn):
    store = OnchainStore(conn)
    observe(
        store,
        {
            "head_block": 200,
            "coverage": [
                {
                    "address": A,
                    "operation": "address-transactions",
                    "from_block": 0,
                    "to_block": 200,
                    "complete": True,
                }
            ],
        },
        at=1,
    )
    unknown = onchain.address_observations(conn, NS, ETHEREUM, B)
    assert (
        unknown["status"] == "unknown"
        and "not the same as no activity" in unknown["note"]
    )
    quiet = onchain.address_observations(
        conn, NS, ETHEREUM, A, from_block=10, to_block=150
    )
    assert quiet["status"] == "no_observed_activity_in_covered_range"
    assert not [u for u in quiet["unknowns"] if u["kind"] == "uncovered_block_ranges"]
    beyond = onchain.address_observations(
        conn, NS, ETHEREUM, A, from_block=150, to_block=260
    )
    gaps = [u for u in beyond["unknowns"] if u["kind"] == "uncovered_block_ranges"][0][
        "ranges"
    ]
    assert gaps == [{"from_block": 201, "to_block": 260}]


def test_uncovered_ranges_merge_windows_and_ignore_incomplete_ones():
    windows = [
        {"from_block": 0, "to_block": 99, "complete": True},
        {"from_block": 150, "to_block": 200, "complete": True},
        {"from_block": 100, "to_block": 149, "complete": False},
    ]
    assert onchain.uncovered(windows, 0, 250) == [
        {"from_block": 100, "to_block": 149},
        {"from_block": 201, "to_block": 250},
    ]
    assert onchain.uncovered(windows, 10, 50) == []


def test_invalid_identifiers_and_names_are_refused(conn):
    OnchainStore(conn)
    assert (
        onchain.address_observations(conn, NS, ETHEREUM, "vitalik.eth")["code"]
        == "name_lookup_refused"
    )
    assert (
        onchain.address_observations(conn, NS, ETHEREUM, "someone@example.org")["code"]
        == "person_identifier_refused"
    )
    assert (
        onchain.address_observations(conn, NS, "solana", A)["code"]
        == "unsupported_chain"
    )


def test_writes_need_the_write_scope(conn):
    store = OnchainStore(conn)
    with pytest.raises(OnchainError) as exc:
        store.project(
            NS,
            {
                "observation_id": "onchain-obs:x",
                "chain_id": ETHEREUM,
                "facts": {},
                "observed_at_ms": 1,
            },
            principal_id="p",
            scopes={"knowledge:onchain:read"},
        )
    assert exc.value.code == "forbidden"


def _label(conn):
    store = OnchainStore(conn)
    contract = fixture_evm_address("records-contract").lower()
    observe(
        store,
        {
            "labels": [
                {"address": contract, "label": "Exampla Token", "label_id": "token"}
            ]
        },
        at=1,
        provider="ethereum-lists-tokens",
    )
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT NOT NULL, "
        "entity_type TEXT, created_at BIGINT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('org:exampla','Exampla Foundation','organization',1),"
        "('person:jane','Jane Example','person',1),('untyped:x','Something',NULL,1)"
    )
    return store, f"{ETHEREUM}|{contract}|ethereum-lists-tokens|token"


def test_label_references_refuse_persons_and_untyped_entities(conn):
    store, key = _label(conn)
    with pytest.raises(OnchainError) as exc:
        store.propose_label_reference(
            NS, key, "person:jane", reason="r", principal_id="p", scopes=WRITE
        )
    assert exc.value.code == "person_reference_refused"
    with pytest.raises(OnchainError) as exc:
        store.propose_label_reference(
            NS, key, "untyped:x", reason="r", principal_id="p", scopes=WRITE
        )
    assert exc.value.code == "entity_type_refused"
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-onchain-label-reference-v1.json"
        ).read_text()
    )
    assert "person" not in schema["properties"]["entity_type"]["enum"]


def test_label_reference_review_and_revert_never_reactivates(conn):
    store, key = _label(conn)
    ref = store.propose_label_reference(
        NS,
        key,
        "org:exampla",
        reason="the label names the foundation's token",
        principal_id="p",
        scopes=WRITE,
    )
    assert ref["state"] == "candidate"
    assert onchain.references_for_entity(conn, NS, "org:exampla") == []
    accepted = store.review_label_reference(
        NS, ref["reference_id"], "accept", "checked", principal_id="r", scopes=REVIEW
    )
    assert accepted["state"] == "accepted" and accepted["decision_id"].startswith(
        "entity-decision:"
    )
    assert [
        r["reference_id"]
        for r in onchain.references_for_entity(conn, NS, "org:exampla")
    ] == [ref["reference_id"]]
    reverted = store.revert_label_reference(
        NS, ref["reference_id"], "wrong token", principal_id="r", scopes=REVIEW
    )
    assert reverted["state"] == "candidate" and reverted["decision_id"] is None
    assert onchain.references_for_entity(conn, NS, "org:exampla") == []
    # Proposing again does not resurrect the earlier acceptance.
    assert (
        store.propose_label_reference(
            NS, key, "org:exampla", reason="again", principal_id="p", scopes=WRITE
        )["change"]
        is None
    )
    assert onchain.references_for_entity(conn, NS, "org:exampla") == []
    assert [
        h["state"] for h in store.label_reference(NS, ref["reference_id"])["history"]
    ] == ["candidate", "accepted", "candidate"]
    with pytest.raises(OnchainError):
        store.review_label_reference(
            NS, ref["reference_id"], "accept", "x", principal_id="r", scopes=WRITE
        )


def test_labels_are_quoted_with_attribution(conn):
    store, _ = _label(conn)
    contract = fixture_evm_address("records-contract").lower()
    labels = onchain.labels_for(conn, NS, ETHEREUM, [contract])[contract]
    assert labels[0]["quote"] == "Exampla Token" and labels[0]["stated_by"] == "Fixture"
    assert labels[0]["framing"].startswith("label as stated by the source")
    assert store  # the store stays usable
