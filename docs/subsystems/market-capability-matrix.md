# Market capability matrix

This matrix is the source of truth for what the local Noesis market domain can
prove. `fixture-verified` means deterministic local evidence exists;
`credential-blocked` means the code has an explicit adapter/rights boundary but
no usable provider credentials or license decision is available;
`live-partial` means a hash-only provider sample exists but coverage or rights
are incomplete; `live-verified` applies only to the exact scope named by its
receipt. `not-implemented`
must not be inferred from an empty warehouse.

| Asset/capability | Instruments | Provider/source | History/freshness | Analytics | Rights/status | Known exclusion |
| --- | --- | --- | --- | --- | --- | --- |
| US equities | issuer/security/listing/universe revisions | live SEC evidence + FMP EOD adapter | dated EOD bars, as-of/acquisition clocks | metrics, dashboards, screeners, event/factor/backtest/portfolio/risk | fixture-verified; FMP `live-incomplete`; entitlement checked at use | supplied FMP scope covers only 2/5 sampled price histories; commercial retention/display/export rights are pending |
| Company dashboard UI | same-origin workspace over authenticated market API | authorized market stores and formula registry | user-selected public/acquisition cutoffs | interactive price chart, fact/source drilldowns, metrics, actions, peer exclusions and freshness | route/assets and fixture dashboard tests pass | live company coverage and analyst acceptance remain unverified; standard market-cap multiples need sourced numerator facts |
| EDGAR financial statements | CompanyFacts normalization plus bounded native-context Inline XBRL parser and filing reconciliation | SEC submissions, CompanyFacts and accession-scoped filing document | accession, accepted/public and acquisition timestamps; context-specific periods retained | statement mapping diagnostics, value reconciliation, missing/conflict/unmapped reporting | offline fixtures pass; live SEC reconciliation of the five issuers' latest 10-K/10-Q matched 4,897/4,897 comparable facts (`live-sec-statements.json`) | segment/extension facts are counted, not compared; no analyst review is claimed |
| Economic releases | series/vintage/release snapshots | retained connectors; live FRED current/vintage/date-only calendar receipt | provider release vs local acquisition clocks | calendar, initial/latest comparison, timestamped consensus, breadth | fixture-verified; FRED sample `live-verified`; consensus requires current entitlement | FRED calendar evidence is date-only; no intraday release time is inferred |
| International equities | market matrix, taxonomy/cross-listing/translation contract | credential-blocked until coverage/cost review | fixture matrix only; no representative live replay | explicit missing-field and accounting-standard reporting | credential-blocked | IFRS/local taxonomy and withholding rules need a reviewed source pack |
| Fixed income | normalized bond terms and cash-flow receipts | credential-blocked | fixture yield/duration/convexity only | clean/dirty price, accrued interest, curve spread and stress; callable/structured limits visible | credential-blocked | licensed terms, settlement calendars, independent pricing |
| FX/commodities | quote and contract metadata accepted | credential-blocked | fixture spot/futures/negative-price replay only | direction-safe conversion, raw-contract P&L and synthetic roll separation | credential-blocked | delivery, rolls, venue calendars and supply/demand evidence |
| Options/derivatives | dated contract terms and chain rows accepted | credential-blocked | fixture Black-Scholes replay only | Greeks, bounded IV, surfaces and scenarios; unsupported exercise styles visible | credential-blocked | licensed chains, corporate actions and model validation |
| Digital assets | chain/address/wrapped-asset identity and event receipts | credential-blocked | fixture 24/7/reorg/supply replay only | venue disagreement, supply history and reliability indicators; no liquidity guarantee | credential-blocked | indexer/finality, protocol evidence and source reliability |
| Intraday | bounded sequence/recovery event contract | credential-blocked | fixture ordering/gap/latency replay only | corrections, late events, p95 latency and backpressure; no real-time SLA claim | credential-blocked | exchange reconnect, retention/cost benchmark and entitlement |
| Operations/SLOs | normalized measurement, budget, backup and recovery receipts | local operations store; provider adapters submit observations | freshness, coverage, latency, job/delivery, throughput and cost observations | target evaluation, provider health, budget enforcement and replay drills | namespace/scope checked; local capacity receipt recorded; missing telemetry is degraded/no-data | measured local run is single-process only; production capacity, restore timing and infrastructure cost require deployment evidence |

Every claimed expansion must attach the acceptance-pack receipt containing
contract validation, an independent reference calculation, historical replay,
entitlement result, and user-workflow result. Missing credential or human
review evidence remains a visible blocker.

The core fixture pack declares the provisional MSFT/ORCL/CRM/ADBE/NOW universe
and runs a deterministic lookup → four-peer dashboard → macro dashboard →
historical screen → alert journey with source/run receipts. It also writes and
exports a cutoff-pinned comparison, reopens the database, checks that later
provider corrections do not rewrite the saved report, and verifies that
unverified rights withhold external payloads. Missing valuation inputs remain
visible instead of being inferred from price. These checks verify local fixture
behavior. Separate hash-only live receipts cover SEC filings, FRED macro data
and partial FMP access; analyst usefulness and complete commercial provider
rights remain separate gates.
