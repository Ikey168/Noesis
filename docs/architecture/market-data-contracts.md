# Market data contracts and architecture

**Status:** additive v1 U.S. equity contract and implementation baseline; provider-specific ingestion remains gated on authorized samples.  
**Roadmap:** [market analytics and research](https://github.com/Ikey168/Noesis/issues/1647), phases [#1649](https://github.com/Ikey168/Noesis/issues/1649) and [#1650](https://github.com/Ikey168/Noesis/issues/1650).  
**Architecture issue:** [#1656](https://github.com/Ikey168/Noesis/issues/1656).  
**Provider evaluation:** [#1655](https://github.com/Ikey168/Noesis/issues/1655).

## Design rules

1. **Names are not identities.** Issuers, securities, and exchange listings have distinct stable IDs. A ticker is a dated listing attribute; it must never be the sole security key. The security/listing store links an issuer to the Knowledge Graph but does not duplicate the KG's organization identity.
2. **Keep source observations separate from calculations.** Provider bars, quotes, actions and reported filing facts are immutable source observations. Returns, valuation ratios, adjustments and analysis belong to the existing quantitative calculation path and cite the exact input revision IDs.
3. **Preserve three clocks.** Each source value records its subject/effective time, provider publication/filing time if supplied, and local retrieval time. Historical queries state the effective-time selector, “public by” cutoff and “acquired by” cutoff. A current value is not silently used to fill a missing historical vintage.
4. **Keep a raw source receipt.** Store the provider record identity, provider revision when supplied, content/source snapshot, retrieval time, license/entitlement reference, and locator. A later correction appends a revision. Never overwrite or silently rewrite the original provider payload.
5. **Preserve units, currencies and adjustment basis.** Raw and adjusted prices are distinguishable. Dividend and split adjustments carry their method, cutoff and action inputs. Cross-currency arithmetic requires a versioned FX observation through `QuantitativeStore`.
6. **Keep outputs rights-aware.** Every third-party numeric record carries an entitlement reference. The provider agreement controls storage, cache duration, display, derived data and export. Missing permission blocks the restricted operation, not provenance capture allowed by the agreement.
7. **Keep user work separate.** Public/shared issuer, listing and market observations live in the market data namespace when the applicable license permits it. Watchlists, holdings, user choices and research questions remain owner-scoped and namespace-scoped.

## Contract set

The draft-07 schemas under `contracts/schemas/jsonschema/` establish the v1 payload boundaries. The schemas validate object shape; runtime stores must still enforce immutable revisions, namespace authorization, point-in-time selection and current provider entitlements.

Synthetic examples for the market contracts are in `contracts/examples/noesis-market-*-v1.json`. They are shape fixtures only; they are not provider samples or evidence of vendor coverage.

| Object | Contract/schema | Stable identity and revision | Authoritative owner |
|---|---|---|---|
| Issuer/company | `noesis-market-issuer-v1` · `noesis-market-issuer-v1.json` | Stable `issuer_id` and linked `kg_entity_id`; names and external identifiers are dated assertions in numbered issuer revisions. Do not make CIK or company name the only internal identity. | Knowledge Graph owns the organization entity and cross-domain relations. Market store owns finance-specific identifiers and dated market assertions, linked to the KG entity. |
| Security | `noesis-market-security-v1` · `noesis-market-security-v1.json` | Stable opaque `security_id`, issuer link, asset/capital class and revision. CUSIP/ISIN/FIGI/provider identifiers are attributed, dated aliases. | New market instrument registry. Ticker symbols do not identify the security. |
| Listing | `noesis-market-listing-v1` · `noesis-market-listing-v1.json` | Stable opaque `listing_id`, security, MIC, currency, and valid-time interval; each revision records its known-as-of/retrieved clocks. Ticker changes are dated listing aliases; a change of venue is a separate listing. | New market instrument registry. |
| EOD/intraday bar | `noesis-market-bar-v1` · `noesis-market-bar-v1.json` | Composite natural key: provider + listing + interval + bar-start time + provider revision. Keep a content revision even if the vendor supplies no revision ID. | Market observation store backed by the existing local-first warehouse and snapshot/lineage primitives. |
| Trading session | `noesis-market-trading-session-v1` | Calendar + MIC + local session date + revision; separates exchange closure from data coverage (`present`, `no_trade`, `missing`, `not_requested`, `unknown`). | Market observation store; venue timezone comes from the listing/calendar contract. |
| Quote | `noesis-market-quote-v1` · `noesis-market-quote-v1.json` | Provider + listing + quote time + venue/condition + source revision. Quote is an optional later capability; it is not needed for the end-of-day beta. | Market observation store, with short-lived cache only when provider terms allow it. |
| Corporate action | `noesis-market-corporate-action-v1` · `noesis-market-corporate-action-v1.json` | Stable action identity + source revision; issuer/security/listing links, event dates, split/share ratios, dividend basis/class, related-security links and correction/cancellation status. | `MarketCorporateActionStore`, linked to immutable provider source receipts. |
| Adjustment calculation | `noesis-market-adjustment-calculation-v1` · `noesis-market-adjustment-calculation-v1.json` | Formula version, public/acquisition cutoffs, source entitlement IDs, exact bar/action revisions, raw closes and versioned split/total-return factors. | Market result store enforces market entitlements; the existing `QuantitativeStore` owns the shared calculation receipt and audit lineage. |
| Reported financial fact | `noesis-market-financial-fact-v1` · `noesis-market-financial-fact-v1.json` | Source accession + taxonomy + concept + context key/unit + exact lexical value, statement mapping, actual period dates and revision. CompanyFacts-derived keys identify themselves as composites, not native `contextRef` values. | Filing/document ingestion owns the filing text/source revision. `MarketFinancialFactStore` indexes the tagged observation and links to the SEC accession. |
| Issuer relationship | `noesis-market-issuer-relationship-v1` | Directed, revisioned relationship with effective date and source provenance. Mergers and spin-offs retain both issuer identities. | Market instrument master; KG can link to the issuer IDs without owning the market relationship record. |
| Universe membership | `noesis-market-universe-membership-v1` | Universe + security + revision, with valid interval, inclusion/exclusion and source receipt. Never delete prior membership. | Market instrument master; a screened universe pins the membership revision used. |
| Economic release | Existing `noesis-economic-release-snapshot-v1` / comparison contracts and `EconomicReleaseStore`. | Reuse existing release, provider-vintage and retained-observation identities. | Economic domain. Do not create a parallel market-specific macro store. |
| Cross-domain as-of manifest | `noesis-market-asof-snapshot-v1` / `MarketAsOfSnapshotStore`. | Immutable selection of existing listing, bar, action, fact, economic-snapshot, document and calculation revisions under explicit effective/public/acquisition cutoffs. | New market service references authoritative source stores; it does not copy their payloads. |
| Quality assessment, quarantine and repair | `noesis-market-quality-assessment-v1`, `noesis-market-quality-quarantine-v1`, `noesis-market-quality-candidate-v1`, `noesis-market-repair-plan-v1` / `MarketQualityStore`. | Assessment hashes pin source revision references and findings; review decisions target exact revisions; repair plans pin a bounded request and existing ingest checkpoint. | Quality findings and review events are additive receipts; source payloads remain in the price, action and filing-fact stores. |
| Provider entitlement policy | `noesis-market-entitlement-v1` / `MarketEntitlementStore`. | Namespace-scoped append-only license policy revisions carry provider/license identity, effective/expiry clocks, operation capabilities, optional retention age, reviewer and evidence references. | Current policy is resolved at each market store operation; source references are checked against current provider and license identity. |
| As-of provenance export | `noesis-market-asof-export-v1` / `MarketAsOfSnapshotStore.export_snapshot()`. | Exports revision/hash provenance only; records the current entitlement decision for each included market source. External export additionally requires redistribution permission. | It does not include source payloads. It is not a replacement for future report or Evidence Bundle export adapters. |
| Quantitative calculation | Existing `noesis-quantitative-calculation-v1` and `QuantitativeStore`. | Stable calculation receipt pins exact input observation IDs, formula revision, unit/rounding rules and output hash. | Quantitative subsystem. It computes market metrics; other surfaces do not reimplement formulas. |
| Market metric report | `noesis-market-metric-report-v1` / `MarketMetricStore`. | Formula versions, exact issuer/listing/security, bar/action/fact/source revisions, as-of cutoffs, units, current derive-policy revisions and shared calculation receipts. | The market domain composes existing stores; filed-fact scalar formulas are registered/evaluated through `QuantitativeStore`, and price formulas use its shared domain-calculation ledger. |
| Research run/artifact | Existing Research Project, Deep Research, authored-report, Evidence Bundle and intake session contracts. | Reuse project/session/artifact identity and revision. Pin query cutoffs, universe revision, source revisions, calculation receipts and entitlement decisions in linked run evidence. | Existing Research/Creation/Intake owners retain their authoritative records. A market brief is not a duplicate research-project store. |

### Financial fact period rules

An instant fact (for example, cash and equivalents at a balance-sheet date) has one `instant_date`. A duration fact (for example, quarterly revenue) has a `start_date` and `end_date`. Store the taxonomy/concept, unit, scale/decimals, fiscal label, filing form and accession, accepted/filed timestamp, source-document revision and locator. When an upstream CompanyFacts record omits native `contextRef`, mark the `context_id_kind` as a composite key and do not label it a native filing context. Do not combine facts with different fiscal periods or contexts simply because their display labels match. See the [EDGAR financial facts guide](../guides/market-financial-facts.md).

The legacy SEC mapper in `src/ingestion/connectors/edgar.py` still reduces five concepts to the latest filed value for the filing-note/series workflow. The new `companyfacts_to_market_facts()` path and `MarketFinancialFactStore` preserve accession/context revisions, statement mappings, exact values, actual periods and point-in-time selection. SEC CompanyFacts does not provide native context IDs or entity-dimensional/custom facts; full filing reconciliation and native context recovery remain acceptance work in [#1660](https://github.com/Ikey168/Noesis/issues/1660).

### Macro-series provider clocks

Official macro series continue through the existing `DatasetConnector`, `ObservationStore`, and `EconomicReleaseStore`. Connector records preserve provider-specific vintage/update clocks, local acquisition time, units, geography, frequency, seasonal-adjustment metadata, and provider dimensions where available. FRED accepts an explicit ALFRED `vintage_date` and can return source-published release dates at day precision; those dates are not exact availability timestamps. Eurostat update time, World Bank `lastupdated`, and generic SDMX header preparation/extraction clocks are retained as provider metadata and are not asserted as official release times. `harvest_with_report()` surfaces missing credentials, skipped series, empty data and provider errors without copying exception text or credential-bearing request URLs into the report. Release snapshots surface the basis for their release, vintage and acquisition clocks. See the [economic provider vintage guide](../guides/economic-provider-vintages.md) and [#1661](https://github.com/Ikey168/Noesis/issues/1661).

### Point-in-time query request

Every future history/screen/research request accepts a common temporal selector:

```json
{
  "effective_at_ms": 1790208000000,
  "publicly_available_by_ms": 1790208000000,
  "acquired_by_ms": 1790208000000,
  "universe_revision": "universe:enterprise-software@1",
  "adjustment_basis": "unadjusted"
}
```

The effective-time selector and both millisecond cutoffs are required for historical replay. `MarketAsOfSnapshotStore` implements this policy for listings, bars, corporate actions, reported facts, retained economic snapshots, documents and saved calculation receipts. It pins exact revision IDs and hashes in an immutable manifest, reports known gaps, and rechecks source access when the manifest is inspected. `universe_revision` pins membership so a current constituent list cannot leak into historical backtests. `adjustment_basis` must be one of the declared methods and adjustment cutoff. The service and its coverage limits are documented in the [market as-of snapshot guide](../guides/market-asof-snapshots.md).

### Source provenance and entitlement

Each ingested payload includes provider name, provider object ID, provider revision (nullable if absent), source URL/locator, public/release timestamp when known, fetched timestamp, source content hash or snapshot ID, license ID, entitlement ID, and declared correction/version behavior. The entitlement ID resolves through the access and license system; it is not a bearer token and does not itself grant permission. If a provider has no source-revision identifier, Noesis derives a local content revision and retains the raw bytes only for the duration permitted by the agreement.

## Authoritative system boundaries

| Concern | Authority | Market integration |
|---|---|---|
| Organization/person identity and relationships | `src/knowledge_graph/` | Link `issuer_id` to `kg_entity_id`; never create a second organization graph. Security/listing IDs stay in the market registry. |
| Filing documents and source revisions | document ingestion and integrity/snapshot stores | Store accession and source-document revision/locator; enrich from EDGAR and other licensed filing sources. |
| Statistical series and macro releases | `src/ingestion/connectors/dataset/`, `ObservationStore`, `src/domains/economic/` | Reuse macro connectors, release snapshots and revision comparisons. Do not encode OHLCV bars as five unrelated synthetic company identities in `dataset-series-v1`. |
| Equity/security/listing registry and rich market observations | new finance/market domain tables under the existing warehouse connection | Add additive tables and explicit migration/version policy for issuers-as-market-identifiers, securities, listings, bars, quotes, corporate actions and reported-fact indexes. Do not fork the warehouse or duplicate corpus tables. |
| Formulas, units, FX conversion, derived series | `src/kb/quantitative.py` / `QuantitativeStore` | Register versioned market measures there; market tools call the shared calculator and return receipts. |
| Research questions, sessions and report artifacts | Research Project, ten-mode intake, Creation/authored reports, Evidence Bundle | Link exact object revisions and calculation receipts. Do not introduce `market_research_runs` as another project ledger. |
| Provider configuration, connectors and execution | existing ingestion/pipeline/source-pack and lineage machinery | New providers declare credentials, terms, entitlement, rate limits, bounded requests, retry policy and source revisions before execution. |
| Authorization and user state | current access scopes, namespaces and owner-scoped stores | Public market data are readable only where the provider grants the applicable use. User portfolios, alerts and research remain tenant/owner scoped. Read, write, calculate and admin operations use explicit market scopes plus namespace scope. |
| User and agent interfaces | shared Python market service | MCP and REST are thin adapters to the same service. MCP registers typed tools through the capability catalog; REST routes call those service operations. No separate calculation or entitlement implementation in frontend/routes. |

## Proposed implementation boundaries

```text
Provider API / authorized upload
       │
       ▼
Market connector ──► raw response snapshot + acquisition receipt
       │
       ├──► Issuer/security/listing registry ──► Knowledge Graph issuer link
       ├──► source market bars/actions/facts ───► market tables in local warehouse
       └──► filings/macroeconomic series ───────► existing document/economic stores
                      │
                      ▼
          shared market query/service layer
              ├──► QuantitativeStore (calculations and receipts)
              ├──► Research / Evidence Bundle (versioned analysis)
              ├──► MCP adapter + capability catalog
              └──► REST adapter ──► web interface
```

Schemas and synthetic contract examples live in `contracts/schemas/jsonschema/` and `contracts/examples/`. The issuer/security/listing master is implemented in `src/domains/market/instruments.py`; revisioned OHLCV bars, session calendars and bounded page checkpoints are implemented in `src/domains/market/prices.py`; corporate actions and adjustment results are implemented in `src/domains/market/actions.py`; filed-fact indexing is implemented in `src/domains/market/financial_facts.py` and fed by `src/ingestion/connectors/edgar.py`. Cross-domain point-in-time composition is implemented in `src/domains/market/asof.py`. These use additive tables in the existing DuckDB connection, with calculation receipts recorded through `QuantitativeStore`. Vendor-specific market adapters belong under `src/ingestion/connectors/market/` after provider terms and sample payloads pass #1655. Keep the economics pack authoritative for macro releases. MCP wrappers belong beside the existing Knowledge Engine tools. REST routes are adapters only. Existing `DatasetConnector` may be reused for provider lifecycle patterns, but bars/quotes need rich typed records beyond scalar `SeriesRecord.observations`.

## Versioning, time and migrations

- Each object has a stable ID plus a positive local `revision`; every mutation appends an immutable revision and a content hash. Source revisions are separately identified from Noesis object revisions.
- Use provider/effective time, public release/filed time and local retrieval time as separate fields. Historical joins require an explicit public-availability and acquisition cutoff.
- A correction creates a new observation/object revision and references the prior revision. Retain raw price values, actions and input revisions; calculated adjustment factors/ratios are separate versioned calculations.
- Database changes are additive: create namespaced market tables/indexes, do not alter or reinterpret `dataset-series-v1`, filing series, document identities or old snapshots. Schema migration is reversible before any production write; the rollout must be idempotent and must preserve old rows.
- Missing old history is a first-class response with a reason. Never backfill earlier provider availability or financial facts from today's values without marking a current-only reconstruction.
- Access checks run when records are queried, exported, replayed or attached to a report; a prior authorization result is not a durable entitlement.

## Shared Python, MCP and REST contract

Create one typed application service for: issuer/security/listing resolution; bounded date-range history; corporate-action/fact reads; point-in-time screen calculation; shared quantitative formulas; and research evidence assembly. Each operation returns a versioned payload plus provenance, selected revisions, entitlement/access status, temporal policy and honest missing-data envelope. Writes require current namespace and provider-specific authorization, idempotency key, expected current revision and an audit receipt.

- **Python:** service/store calls accept an explicit `namespace`, principal/access context, bounds, and typed request; no hidden global provider credentials or unbounded query.
- **MCP:** expose the same operations as discoverable typed tools with scope/readiness metadata. MCP tools do not reimplement SQL, metric formulas, or license checks.
- **REST:** expose the same application service under authenticated API routes, preserving response contracts and error codes. REST does not bypass MCP/service authorization or source rights.
- **UI:** request the same REST/MCP payloads, render returned freshness, citation, revision and missing-data fields, and avoid locally recomputing official values.

Suggested initial read tools are `market_resolve_instrument`, `market_history`, `market_facts`, `market_actions`, `market_compare`, and `market_screen`. Suggested writes are `market_ingest_preview`, `market_ingest_apply`, and `market_watch_upsert`. These are names for design, not implemented or registered tools yet; contract and entitlement checks must exist before public registration.

## Local-first storage decision and benchmark gates

Keep DuckDB as the initial warehouse and use the current `ObservationStore`, snapshot/lineage, economic, quantitative and KG stores as mapped above. Rich equity bars and facts need additive market tables, not a new analytics warehouse. Existing DuckDB is a local single-writer store; do not claim concurrent multi-host write safety.

Before replacing/adding a warehouse, benchmark a reproducible local profile with 16 GiB RAM, four CPU cores and local SSD against the following provisional beta thresholds:

| Workload | Data set | Initial target |
|---|---:|---:|
| Read one issuer's ten-year daily bar history | ≈2,520 bars | p95 ≤ 250 ms warm; p95 ≤ 1 s cold |
| Five-issuer ten-year comparison plus annual/quarterly facts | ≈12,600 daily bars plus <2,000 facts | p95 ≤ 2 s warm; p95 ≤ 5 s cold |
| Idempotent daily provider upsert | 100 issuers / ≤100,000 new or revised bars | complete in ≤60 s without unbounded memory growth |
| Local database footprint for the 100-issuer ten-year beta, indexes and receipts | measured fixture/live-equivalent sample | ≤2 GiB, excluding raw source snapshots whose license requires separate object storage |

These are acceptance targets, not measured results. First measure the actual query and ingestion implementation. If query targets fail after query/index work, storage exceeds the documented profile, or licensing requires separate immutable/object storage, present the measured gap before choosing a new backend. If later scale exceeds the local writer's operational envelope, retain the contracts and move only the relevant observation tables, with parity/replay evidence. Do not move data to a managed service just because the projected all-market row count is large.

## Authorization model

Use current principal and namespace scopes for user state plus market capability scopes (read, write, calculate, administer). Resolve source-specific provider entitlements on each operation. An installed connector, current source snapshot, prior calculation, or shared/global market object does not by itself allow the caller to see raw data or redistribute derived data. Owner-scoped watchlists/holdings and private research inputs must not leak into global market queries or another tenant's results. Admin and operator scopes do not override provider restrictions or grant redistribution rights. The entitlement store, current checks and bounded source-revision retention purge are documented in the [market data entitlements guide](../guides/market-data-entitlements.md).

The policy store and local-domain enforcement boundary are implemented under [#1664](https://github.com/Ikey168/Noesis/issues/1664). Market REST/MCP operations now recheck current rights through the shared domain service; vendor-populated terms, cache/report/Evidence Bundle integration and external provider review remain open.

## Rollout and validation evidence

The architecture issue established these contracts and boundaries. The instrument master implements issuer, security, listing, issuer-relationship, alias-review and dated-universe records with fixture validation. The price layer accepts normalized bars, tracks session coverage and provides bounded/resumable ingestion checkpoints. The corporate-action layer retains revisions and calculates split-aware price-return and dividend-reinvested total-return series with fixture validation. The filing-fact path indexes SEC CompanyFacts observations with fixture validation, accession history, date semantics and mapping diagnostics; native XBRL contexts and filing reconciliation remain open. The cross-domain as-of manifest service now composes existing source revisions with explicit gaps and transformation replay checks; its focused tests are fixture-only, and a selected price range remains partial without calendar-proven coverage. A shared capability service now exposes lookup, history, filings, economic snapshots and metrics to REST and MCP and feeds the generated catalog under [#1666](https://github.com/Ikey168/Noesis/issues/1666). No vendor market-data adapter or live price/action sample is configured; UI and research workflows remain later phase work.

The market quality layer adds revision-pinned price and filing-fact assessments, audited quarantine/release decisions, sanitized invalid-row candidates and bounded price repair plans. Price, corporate-action and fact queries filter active quarantines; company dashboards and market metric receipts expose a revision-level quality summary. This is fixture-validated local domain functionality only: broad reconciliation, live outage tests and provider-backed human review remain open under [#1663](https://github.com/Ikey168/Noesis/issues/1663).

The entitlement layer stores current rights as reviewed revisions, fails closed for absent or revoked source policies, gates derived calculations and provenance export, and supports bounded source-revision purges that leave hash-only tombstones. Market REST/MCP reads and calculations reuse these current checks, and market brief export rechecks source entitlement references. No selected commercial provider rights matrix has been installed; cache, authored-report and full Evidence Bundle adapters still need explicit rights integration under #1678.

Before those features are reported ready, validate all JSON schemas against versioned examples; migration against an existing warehouse; issuer/security/listing joins across ticker change, relisting, merger and delisting; duplicate/corrected/out-of-order provider records; point-in-time cuts with late acquisition and amended filing; and read/write/export behavior after access revocation. Keep contract/fixture results separate from licensed provider samples and analyst acceptance.
