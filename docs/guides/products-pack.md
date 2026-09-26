# Products pack

Find EU-market electronic displays, inspect source-linked specifications and
datasheets, and compare explicitly resolved models. Records come from two
providers and keep their origin:

| Provider | What it contributes | Assertion kind |
| --- | --- | --- |
| Open Icecat (`icecat`) | Brand-authorised descriptions, specifications and datasheet links for the Open catalogue | `brand-authorised-content` |
| EPREL public API (`eprel`) | Supplier registrations: model identifier, energy-label parameters, product information sheet and label | `supplier-registration` |

Neither is independent testing, and a comparison is not a buying
recommendation. Tracking: [#1706](https://github.com/Ikey168/Noesis/issues/1706).

## Install and configure

1. Install the source pack `config/source_packs/products.json`
   (`install_source_pack`) and the domain pack `packs/products/pack.json`.
2. Record the source terms for each source (`accept_source_pack_license`).
   The terms fields say "operator must confirm": Open Icecat storage/export
   and EPREL reuse terms are not verified by this repository.
3. Credentials, by reference only:
   - `NOESIS_EPREL_API_KEY` is **required**. It comes from the EPREL public
     API request process (<https://eprel.ec.europa.eu/screen/requestpublicapikey>).
     Browsing the public EPREL site does not make the API ready.
   - `NOESIS_ICECAT_API_TOKEN` is optional; Open Icecat is read with the
     `openicecat-live` shop name. Full Icecat content is out of scope and is
     reported per model as `outside_open_catalogue`.
4. Replace the pinned selection with your reference cohort. Each source
   declares `product.selection`: 1–50 selectors (`brand` + `product_code`,
   `gtin`, or `registration_number` for EPREL) and a pinned category
   (`provider_category_id` for Icecat, `product_group` for EPREL). There is no
   catalogue crawl and no free-text acquisition.

`products_readiness` reports fixture and live readiness **per provider**
(pack state, missing credential, unaccepted terms). One ready provider never
implies cross-source validation; that stays `outstanding` until a dated live
run of both.

## Acquisition, refresh and recovery

Run the pack with the shared source-pack tools (`run_source_pack_execution`,
pack `products-displays`, operation `models`). Each page fetches one selected
model, so request, byte, result and page budgets, retries, quarantine and
checkpoints are the runtime's.

- **Per-model outcomes are receipts, not failures:** `returned`, `not_found`,
  `outside_open_catalogue` (`product_selection_outcomes`).
- **Run failures are classified:** `authentication_failed` (missing,
  unapproved or revoked key), `rate_limited` (with `Retry-After`),
  `source_unavailable`, `schema_drift`, `response_too_large`.
- **Revisions:** every distinct provider payload is an immutable revision
  (Icecat `ProductDataUpdated`, EPREL `versionNumber`). A newer declared
  revision becomes current; an older one delivered later never replaces it.
  Superseded assertions stay inspectable (`inspect_product_identity`).
- **Withdrawal:** only an explicit provider status (EPREL `WITHDRAWN`)
  withdraws a record. Absence from a partial, filtered, failed or
  entitlement-limited response, or from a narrowed selection, never implies
  discontinuation. Coverage reports `status_basis` as `provider-declared`,
  `observed`, or `not-observed-in-latest-complete-refresh`.
- **Refresh marker:** it advances only when a complete run has projected every
  selected model, counting models projected since the previous marker. A run
  that crashed and resumed from its checkpoint completes the cycle; a partial,
  failed or cancelled run never advances it.
- **Changed selection:** a new pack version with a different selection cannot
  resume an older generation's cursor. Release it once with
  `SourcePackRuntime.release_stale_checkpoint(pack_id, source_id, …)` (audited;
  it refuses to touch the installed generation's checkpoint).
- **Crash recovery:** projection runs before each page checkpoint and is
  idempotent per revision, so a crash between projection and checkpoint
  replays the page without duplicate identities or assertions.

## Identities and matching

- A **variant** is one provider record (provider + native ID, with its
  market). Variants group under a per-provider **model** keyed by brand and the
  exact designation (MPN / model identifier); Icecat may also declare a
  **family**. Titles are never identities. Original identifiers are kept
  verbatim, including leading zeros, with GTIN states `valid`,
  `invalid_checksum`, `invalid_format` or `absent`. A GTIN shared by variants
  of different models is reported as an `identifier_conflicts` entry, never
  merged.
- Icecat and EPREL models are linked only through reviewed matches
  (`propose_product_matches`, `review_product_match`). Candidates are
  deterministic: `proposed` needs an exact designation match plus
  corroborating diagonal/resolution; suffix-only differences (regional,
  bundle) are `ambiguous`; disagreeing diagonal, resolution or GTINs make a
  candidate `contradicted`, which cannot be accepted. Reviews are append-only;
  a later review reverses an earlier one without deleting history.

## Attributes and comparison

The bounded attribute set is diagonal, resolution, width/height/depth (with
or without stand), on-mode power, energy per 1000 h and energy class. Each
assertion keeps the native name, value and unit, JSON-pointer locator, mode
(`sdr`, `hdr`, `typical`, `with-stand` …), label scheme and provider revision.
Normalized values (cm, mm, W, kWh) come from the shared quantitative store and
carry the conversion's calculation receipt.

`compare_product_models` takes 2–6 resolved **model** IDs. Each column is the
model plus its *accepted* equivalents; pending and rejected matches are listed,
not merged. A row is comparable only when every model has a value on the same
attribute, mode and normalized unit (and the same known label scheme for
energy classes). Disagreeing providers show as `conflict` with both values
side by side; nothing picks a winner. Every value cites its variant, revision,
locator and document.

## Documents

`acquire_product_documents` follows only the provider-linked datasheet,
product information sheet and energy label of one variant, under the source's
`product.documents` policy (`retain`, `allowed_hosts`, `media_types`,
`max_bytes`). The pinned pack sets `retain: false` until the operator confirms
the terms, so links are recorded as `link_only` without a request. With
retention allowed, each fetch records `retained`, `not_permitted` (host or
redirect outside the allowlist), `inaccessible`, `too_large` or `unsupported`.
Identical bytes are one asset shared by all links; changed bytes are a new
version. Extraction failures keep the asset and the structured record.

## Query examples

| Intent | Tool | Arguments |
| --- | --- | --- |
| Find a model by designation | `lookup_product_models` | `{"namespace": "global", "brand": "Exampla", "designation": "EX-27Q4"}` |
| Review cross-provider candidates | `propose_product_matches`, then `review_product_match` | `{"namespace": "global"}` |
| Compare three displays | `compare_product_models` | `{"namespace": "global", "model_ids": ["<model>", "<model>", "<model>"]}` |
| Why did a selector return nothing? | `product_selection_outcomes` | `{"namespace": "global", "run_id": "<run>"}` |

## Supported capabilities

| Capability | Offline fixtures | Live |
| --- | --- | --- |
| Open Icecat acquisition (brand+code / GTIN, Open catalogue) | verified | outstanding |
| EPREL public API acquisition (`electronicdisplays`) | verified | outstanding (API key required) |
| Identities, identifier states and conflicts | verified | — |
| Normalization with quantitative receipts | verified | — |
| Reviewable matching | verified | outstanding (needs overlapping live cohort) |
| Refresh, withdrawal, crash recovery | verified | outstanding |
| Document acquisition policy | verified | outstanding (terms confirmation) |
| Three-model comparison | verified | outstanding |

Fixtures (`tests/fixtures/source_packs/products-*.json`) are **authored**
envelopes for a fictional brand in the documented provider shapes, not
provider captures. The field mapping is re-validated only by a live run.

## Live acceptance

```bash
python scripts/products_live_check.py --cohort cohort.json \
  --output docs/development/products-evidence/live-check-<date>.json
```

`cohort.json` holds real selectors
(`{"icecat": [...], "eprel": [{"registration_number": "..."}]}`). The report
records dated counts, per-selector outcomes, hashes, quota headers and
failure codes, never credentials.

The only live run so far is
`docs/development/products-evidence/live-check-build-environment.json`
(2026-09-26). It ran from a build container whose network policy blocks both
providers, without an EPREL key. Icecat failed with `source_unavailable`,
EPREL was blocked at preflight with `credential_missing`, and two-provider
acceptance remains outstanding.

## Exclusions

Prices, stock, retailer feeds, shopping recommendations, affiliate links,
reviews, inferred compatibility, Full Icecat content, EPREL supplier-only
endpoints, unrestricted mirroring and manufacturer manual/support sources.

## Reuse notes

- The market research relationship model (#1674,
  `src/domains/market/research.py`) holds company relationships only; it has
  no product identity store. The Products pack therefore owns product
  identities and keeps the brand as the provider string. Link a model to a
  company entity through the existing relationship tools rather than a second
  company store.
- Units and conversions reuse `src/kb/quantitative.py`; document links reuse
  the source-pack document store and the runtime's HTTPS transport rules.
