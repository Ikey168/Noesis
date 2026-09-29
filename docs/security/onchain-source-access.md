# On-chain source access audit

Status: B01 of the On-chain Observations pack
([#2054](https://github.com/Ikey168/Noesis/issues/2054), tracker
[#2053](https://github.com/Ikey168/Noesis/issues/2053)). Written on 2026-09-28
from the providers' public documentation as known to the authors. **The build
environment has no network access to the providers**, so every statement about
terms, limits and licences that is marked *(verify)* must be checked against the
live terms before a deployment relies on it. `LIVE_VERIFICATION` in
`src/ingestion/onchain.py` records every selected source as `unverified-live`.

## What this audit decides

Which public ledger data may enter Noesis, from where, under what terms, and
which sources the first version of the pack uses. The pack never attributes an
address to a person and never states who controls an address; this audit
therefore excludes any source whose purpose is identity attribution.

| Source id | Kind | Decision |
| --- | --- | --- |
| `etherscan-v2-mainnet` | explorer (Etherscan-compatible) | **selected** for Ethereum mainnet |
| `blockscout-ethereum` | explorer (Etherscan-compatible) | audited, not selected (documented alternative) |
| `blockstream-esplora-bitcoin` | explorer (Esplora) | **selected** for Bitcoin mainnet |
| `ethereum-lists-tokens` | public label dataset | **selected** |
| `eth-labels` | public label dataset | audited, rejected |
| `etherscan-label-cloud` | label pages | audited, rejected |
| `ofac-sdn-digital-currency-addresses` | sanctions list | excluded (out of scope) |

Bitcoin is a second chain added under this same audit because the declared
`common-input` clustering heuristic only exists on UTXO ledgers.

## Explorers

### Etherscan V2 API (`etherscan-v2-mainnet`) — selected

- **Access model.** REST, `https://api.etherscan.io/v2/api` with `chainid=1`.
  Operations used: `module=account&action=txlist|tokentx`,
  `module=contract&action=getcontractcreation`, and the read-only JSON-RPC proxy
  methods `eth_blockNumber`, `eth_getTransactionByHash`,
  `eth_getTransactionReceipt`, `eth_getBlockByNumber`. No write method exists in
  the adapter (`READ_RPC_METHODS`).
- **Authentication.** API key, required. Held in `NOESIS_ETHERSCAN_API_KEY` and
  sent only as the documented `apikey` query parameter. It is never written to a
  receipt, observation, locator, request log or error. Without it every
  Ethereum operation returns `no_provider_configured` and writes nothing.
- **Rate limits.** Free tier documented as 5 calls per second and 100,000 calls
  per day *(verify)*. The adapter never retries. HTTP 429 or a
  "Max rate limit reached" envelope is a failed receipt.
- **Pagination.** `page`/`offset`, `sort=asc`, with `page*offset <= 10,000`.
  Bounded by the source's `max_pages` and `max_results`. A full last page
  records a truncated (incomplete) coverage window.
- **Envelope.** `{"status","message","result"}`. `status: "0"` with an empty
  list and "No transactions found" or "No records found" is an empty success.
  Other `status: "0"` answers are classified as `rate_limited`,
  `invalid_api_key` or `provider_error`. Proxy answers use JSON-RPC
  `{"jsonrpc","result"}`.
- **Variant shapes.** `getcontractcreation` answers without `blockNumber` or
  `timestamp` (older explorer versions). The adapter then reads the creation
  transaction through the proxy. `tokentx` rows may lack `logIndex`, in which
  case transfers are keyed by their own fields.
- **Redistribution.**
  - *Stored observations.* The Etherscan API Terms require attribution
    ("Powered by Etherscan.io APIs") and restrict bulk redistribution or resale
    of API output *(verify)*. Noesis stores parsed ledger facts (hashes,
    blocks, addresses, integer values). Each fact is cited to the public
    explorer page and the receipted request. Raw responses are hashed, not
    stored. Exports should cite rather than re-publish bulk API output.
  - *Labels.* Etherscan's name tags are not taken from the API.
- **Identifiers.** Address, transaction hash, contract address, chain id
  (`chainid=1`, stored as CAIP-2 `eip155:1`).
- **Retained evidence.** Receipt (request id, input hash, request log without
  the key, byte and request counts, adapter version, licence) and observation
  (parsed facts, raw SHA-256, the chain head the explorer reported).

### Blockscout (`blockscout-ethereum`) — audited, not selected

- **Access model.** The Etherscan-compatible RPC API on `eth.blockscout.com/api`,
  with the same `module`/`action` shape. The selected parsers accept its
  responses. Blockscout additionally states `logIndex` on token transfers.
- **Authentication.** API key optional. Per-instance limits apply without a key
  *(verify)*.
- **Terms.** The explorer is open source (GPL-3.0). The hosted instance has its
  own service terms *(verify)*.
- **Decision.** Kept as the documented alternative explorer. It is not declared
  in the source pack until its hosted terms are verified, so only one explorer
  per chain is live.

### Blockstream Esplora (`blockstream-esplora-bitcoin`) — selected

- **Access model.** REST on `https://blockstream.info/api`:
  - `/blocks/tip/height`;
  - `/address/:address/txs/chain[/:last_seen_txid]`: 25 confirmed transactions
    per page, newest first;
  - `/tx/:txid`.

  Mempool transactions are not acquired.
- **Authentication.** None on the public instance.
- **Rate limits.** Undocumented fair use *(verify)*. HTTP 429 is a failed
  receipt and is never retried.
- **Variant shapes.** Outputs without `scriptpubkey_address` (for example
  `OP_RETURN`) are kept with `address: null`, never dropped. Coinbase inputs
  have no `prevout`.
- **Redistribution.** Public ledger data. The Blockstream service terms apply to
  the service *(verify)*. Parsed facts are stored with explorer citations; raw
  responses are hashed.
- **Identifiers.** Base58 (P2PKH/P2SH, checksum-validated), bech32 and bech32m
  (BIP-173/350, checksum-validated) addresses, txid, CAIP-2
  `bip122:000000000019d6689c085ae165831e93`.

## Label datasets

### ethereum-lists tokens (`ethereum-lists-tokens`) — selected

- **Access model.** One file per token contract on raw.githubusercontent.com:
  `MyEtherWallet/ethereum-lists/master/src/tokens/eth/<EIP-55 address>.json`.
  Each lookup reads one explicitly named address and never enumerates the list.
- **Licence.** MIT (repository `LICENSE`) *(verify)*. Quotation with attribution
  is permitted; the attribution string is recorded on every label assertion.
- **What is quoted.** Only name, symbol, type, decimals and website, *as stated*.
  Support e-mail addresses and social handles are dropped before storage,
  because they can name people.
- **Absence.** HTTP 404 means "not labelled by this source", never "unlabelled".
- **Framing.** A label assertion is what the dataset states. It has no owner
  field. Noesis never restates it as a finding. A reviewer may refer the label
  text to an organization or contract canonical entity through a reversible
  identity decision. Person entities are refused by the code and by the
  `noesis-onchain-label-reference-v1` schema.

### eth-labels (`eth-labels`) — rejected

The repository aggregates labels collected from explorer label pages. The
explorers' terms restrict redistribution of those labels, and a repository
licence cannot grant rights its authors do not hold *(verify)*. Rejected.

### Etherscan label cloud (`etherscan-label-cloud`) — rejected

These are web pages, not an API, and carry no licence for bulk quotation.
Scraping is out of scope. Rejected.

### OFAC SDN digital-currency addresses (`ofac-sdn-digital-currency-addresses`) — excluded

The entries name natural persons and are inputs to screening. Person
attribution and screening determinations are non-goals of this pack. Sanctions
statements belong to the Legal bundle's sanctions feature. Excluded.

## Unavailable-access fallback

- No key: Ethereum operations return `no_provider_configured` and write nothing.
- Provider failure, rate limit, byte-budget overrun, cross-host redirect or
  non-allowlisted host: a `failed` receipt with a classified `failure_type`.
  Earlier observations stay current; nothing is retried automatically. A
  retry needs a new `request_id`.
- Queries never invent coverage. An address no acquisition covered answers
  `status: unknown`. Blocks outside complete windows are listed as
  `uncovered_block_ranges`.

## Fixtures

`tests/unit/onchain/fixture_builder.py` authors one fictional ledger in each
provider's documented response shape. Addresses are Keccak-256 or SHA-256
derivations of labels, with valid EIP-55 and bech32 checksums, and are tied to
no real person. The builder writes:

- `tests/fixtures/onchain/*.json` (native responses);
- `tests/fixtures/source_packs/onchain-*.json` (replays and normalised output);
- the hashes pinned in `config/source_packs/onchain.json`.

This is offline evidence only. It is not live evidence and implies no
comprehensive coverage.
