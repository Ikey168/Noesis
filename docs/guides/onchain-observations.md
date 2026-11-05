# On-chain Observations guide

The On-chain Observations pack (`packs/onchain`, tracker
[#2053](https://github.com/Ikey168/Noesis/issues/2053)) turns one explicit
public-ledger identifier into cited observations:

- an address, a transaction hash or a contract address;
- transfers in and out, counterparties, and a contract's deployer and
  first-funding chain;
- a *probable* address cluster.

Labels are quoted from their sources with attribution. Noesis never states who
controls an address, never links an address to a person, and has no wallet,
key or transaction-submission capability.

## Sources

`config/source_packs/onchain.json` (pack `onchain-observations` 1.0.0)
declares three sources. `FALLBACKS` in `src/ingestion/onchain.py` adds one
fallback provider for Etherscan:

| Source | Chain | Key | Operations |
| --- | --- | --- | --- |
| `etherscan-v2-mainnet` | Ethereum mainnet (`eip155:1`) | `NOESIS_ETHERSCAN_API_KEY` (required) | address-transactions, transaction, contract-origin, address-funding |
| ↳ fallback `blockscout-ethereum` | Ethereum mainnet (`eth.blockscout.com`) | none | address-transactions, address-funding |
| `blockstream-esplora-bitcoin` | Bitcoin mainnet (`bip122:000000000019d6689c085ae165831e93`) | none | address-transactions, transaction, address-funding |
| `ethereum-lists-tokens` | Ethereum mainnet | none | label |

The access audit and the per-provider decisions are in
[`docs/security/onchain-source-access.md`](../security/onchain-source-access.md).
Blockscout is the fallback of `etherscan-v2-mainnet` (FA22,
[#2861](https://github.com/Ikey168/Noesis/issues/2861)); see
[Ethereum without an Etherscan key](#ethereum-without-an-etherscan-key). Two
label sources were rejected (`eth-labels`, the Etherscan label cloud) and one
was excluded (OFAC digital-currency addresses). No source or fallback is
verified live yet (`LIVE_VERIFICATION` in `src/ingestion/onchain.py`).

### Ethereum without an Etherscan key

Etherscan's free tier is capped (3 calls/s and 100,000 calls/day in
`config/source_quotas.json`). Ethereum calls go to the keyless Blockscout
Etherscan-compatible API (`https://eth.blockscout.com/api`) instead when:

- `NOESIS_ETHERSCAN_API_KEY` is not configured (`credential_missing`); or
- the shared free-tier ledger (`src/ingestion/quota.py`) shows that the
  Etherscan budget cannot take the call's requests before a window that does
  not reopen soon, such as the daily limit or a provider `Retry-After` block
  (`budget_exhausted`). The per-second limit is waited out, not routed.

The choice is made once per call, before any request. A call never mixes
providers, and a budget that runs out during a call is a failed receipt.

Blockscout is sent only the actions it is declared to serve
(`PROVIDER_ACTIONS`): `account` `txlist` and `tokentx`, and `block`
`eth_block_number` for the chain head, without `chainid`. So
`address-transactions` and `address-funding` work without a key.
`transaction` and `contract-origin` need actions Blockscout is not declared to
serve (`getcontractcreation`, the `proxy` reads). They answer
`status: "unavailable"` with `failure_type: "unsupported_by_provider"`, never an
empty result, and request and write nothing.

Each receipt keeps `source_id: "etherscan-v2-mainnet"`, the source that was
asked for, and adds:

- `provider`: the provider that answered;
- `fallback`: `null`, or `{from, to, reason, detail}` with reason
  `credential_missing` or `budget_exhausted`.

Each observation records the same `provider` and `fallback`, plus
`requested_source`. Blockscout's publisher, licence and attribution are stored
on the observation, so citations and `trace_artifact` name Blockscout.

Set `NOESIS_ONCHAIN_FALLBACK=off` to disable the fallback. Without a key,
Ethereum calls then return `no_provider_configured` and write nothing.
Blockscout's live behaviour and hosted terms are not verified yet: the build
environment has no network access.

## Acquire (Python API, receipted)

```python
from src.ingestion import onchain as acquire

scopes = {"knowledge:onchain:write"}
acquire.acquire_address_transactions(conn, "onchain", "eip155:1", "0x…", request_id="case-1:addr",
                                     principal_id="analyst", scopes=scopes, from_block=18_000_000)
acquire.acquire_contract_origin(conn, "onchain", "0x…", request_id="case-1:origin",
                                principal_id="analyst", scopes=scopes)
acquire.acquire_address_funding(conn, "onchain", "eip155:1", "0x…", request_id="case-1:hop2",
                                principal_id="analyst", scopes=scopes)
acquire.acquire_label(conn, "onchain", "0x…", request_id="case-1:label", principal_id="analyst", scopes=scopes)
```

Each call follows the same rules:

- It takes one explicit identifier. Names such as ENS names, e-mail
  addresses and handles are refused.
- It runs under the source's budget: a host allowlist, a request cap, a byte
  cap per response and a timeout. It never retries.
- It writes a durable receipt keyed by `request_id`. A replay with the same
  inputs returns the stored receipt; reusing a `request_id` with other inputs
  is refused.
- The API key is sent only as Etherscan's documented `apikey` parameter and is
  never stored or sent to the fallback. Without a key, Ethereum calls use the
  Blockscout fallback where it serves the operation (see above).
- Passing `investigation=` logs the receipt into that investigation's audit
  trail.
- `trace_artifact(document_id="onchain-obs:…")` shows the full chain: the
  source-pack source, the receipt, the observation and the record revisions
  that cite it.

## Records

`src/kb/onchain.py` (contract `noesis-onchain-observation-v1`) owns five record
kinds: transaction, transfer, contract deployment, funding and label
assertion. Each record is revisioned per namespace and keyed with its chain id.

- Re-acquiring unchanged data adds nothing.
- Changed data becomes a new revision. It is current only if its reported
  chain head is not older than the current revision's.
- Late older data is kept as history, not as a correction.
- A return to earlier content is a new `reversion` revision.
- Reorganisations (`reorg`), finality crossings (`finality`: 64 blocks on
  Ethereum, 6 on Bitcoin) and transactions that disappear (`dropped`) are
  explicit changes.

A label reference lets a reviewer state that a source-stated label refers to
an organization or contract canonical entity. Person entities are refused. The
decision is recorded as an entity identity decision, and a revert returns the
reference to `candidate`.

## Query (MCP server `noesis-onchain`, read-only, scope `knowledge:onchain:read`)

| Tool | Answer |
| --- | --- |
| `address_observations(address, chain_id, from_block?, to_block?)` | Cited transactions and token transfers with direction, value, block, timestamp, finality and explorer citation. Coverage windows, uncovered block ranges, truncated windows and the internal-transfer gap are listed as unknowns. An address no acquisition covered returns `status: unknown`. |
| `contract_origin(contract)` | The cited deployment, the contract's first inbound funding and the deployer's first-funding chain. A hop that was not acquired is `not_acquired`. |
| `address_cluster(address, chain_id)` | `status: probable` with members and edges. Each edge names its heuristic and hub and cites its transactions. The answer also carries the threshold and the FPR measured at it, a degree-preserving null model, and the caveat. It has no owner or attribution field. |

Every invocation is logged to a provisioning audit trail kept in a separate
store: `NOESIS_ONCHAIN_AUDIT_PATH`, or `onchain_audit.duckdb` beside the
warehouse. Before any acquisition has run, every tool answers `not_ready`.

## Clustering heuristics and calibration

| Heuristic | Chains | Score | Known failure |
| --- | --- | --- | --- |
| `common-input` | UTXO | 1 / (inputs − 1) | CoinJoin (filtered), PayJoin, custodial batching |
| `deposit-address-reuse` | EVM | 1 / (senders to the deposit address − 1) | hot wallets, bridges, shared processor addresses |
| `deployer-funding` | EVM | 1 / (addresses first-funded by the same funder) | exchange withdrawals and bridges |

`src/kb/onchain_calibration.py` sweeps the threshold over the labelled
synthetic fixture `config/onchain/cluster-calibration.json`. The fixture holds
related pairs and the known false-positive cases: an exchange hot wallet, a
bridge, a payment processor, exchange-funded deployers, a PayJoin and a
CoinJoin.

The served default is `0.3`: the smallest level with FPR ≤ 0.1 and TPR ≥ 0.5.
At that level the measured FPR is 1/14 (the PayJoin, which this heuristic
cannot detect) and the TPR is 9/11. Queries serve these numbers from the
committed report `config/onchain/cluster-calibration-report.json`. A unit test
reruns the calibration and checks that it still equals the report. These rates
come from a synthetic fixture; real-world rates differ.

A label that the dataset stops stating (HTTP 404 on a later lookup) becomes a
`withdrawn` revision. It is no longer quoted, and label references to it stop
resolving.

## OSINT composition

The OSINT bundle's optional `onchain` feature is off by default.
`entity_dossier(entity=<canonical id>, onchain_namespace=…)` adds cited
contract-origin facts for an organization, reached only through accepted label
references. It never matches names, is never assembled for a person, and is
never presented as the organization controlling an address.

## Exclusions

The pack does not do any of the following, and has no tool for them:

- person attribution or de-anonymization;
- wallet or key access;
- transaction signing or submission;
- exchange or KYC data;
- commercial attribution feeds;
- attribution verdicts.

## Offline evidence

- Offline journey:
  `tests/unit/domains/test_onchain_acceptance.py::test_contract_and_address_to_cited_origin_transfers_and_probable_cluster`.
- Fixtures: `tests/unit/onchain/fixture_builder.py`. Every address and hash is
  fictional, with a valid EIP-55 or bech32 checksum.
- Live evidence: none yet (see the audit).
