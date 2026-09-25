# Specialized market analytics

`MarketSpecializedStore` provides bounded, provider-neutral calculations for
the asset classes that are intentionally outside the core equity/EOD stores.
The shared capability service exposes the same calculations over REST and MCP:

| Capability | Local behavior | Explicit production gate |
| --- | --- | --- |
| `market_fixed_income` | Cash-flow normalization, settlement/calendar metadata, issuer/credit evidence, accrued interest, clean/dirty price, yield, duration, convexity, curve spread, and parallel-yield stress | Licensed bond terms and independent prices; callable/structured instruments are labeled unsupported rather than complete |
| `market_fx_commodity` | Quote-direction normalization, currency conversion, raw-contract P&L, negative prices, and synthetic continuous-series metadata | Licensed FX/futures history, delivery/roll evidence, venue calendars, supply/demand research |
| `market_derivatives` | European Black–Scholes price/Greeks, implied volatility, no-arbitrage bounds, surface validation, and scenarios | Licensed chains, contract adjustments, independent cases, and model review for unsupported styles |
| `market_digital_asset` | Explicit native-versus-contract identity, wrapped-token provenance, venue disagreement, supply events, redenomination, reorg history, and reliability indicators | Selected market/on-chain sources, finality/indexer evidence, protocol research, and liquidity review |
| `market_intraday_replay` | Sequence ordering, detected gaps versus recovered and still-unrecovered sequences, duplicate/correction replacement, late events, latency percentiles, rate/backpressure checks, and bounded replay hash | Exchange timestamps, reconnect tests, capacity/cost benchmark, and real-time/delayed rights |
| `market_international_coverage` | Market capability matrix, taxonomy mappings, cross-listings, and translation provenance | Licensed exchange/filing samples, IFRS/local mappings, withholding/corporate-action review |

Every result contains a formula version, source revision IDs, an immutable run
receipt, and a `source_rights` section. A fixture without provider entitlement
references is reported as `rights_unverified`; it is not treated as a provider
license, live coverage, or user acceptance result. If entitlement references are
supplied, the current `MarketEntitlementStore` policy is checked before a
derived result is returned.

Continuous futures output is deliberately split into a synthetic chart series
and raw contract/tradable P&L. A negative commodity price is valid input. FX
rates are normalized to conventional `quote currency per base currency`
without changing the pair identity; the original value and direction remain in
each normalized observation. European option analytics do not
claim support for American, barrier, Asian, or structured products.

The local acceptance path is covered by
`tests/unit/domains/test_market_specialized.py`. It verifies deterministic
receipts, invalid bounds, sequence recovery, negative prices, reorganization
history, and contract/example validation. It does not substitute for the live
provider and analyst evidence required by market issues #1687–#1692.
