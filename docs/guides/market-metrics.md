# Market metric calculations

`src/domains/market/metrics.py` calculates point-in-time daily price metrics and
filed-fact ratios. `MarketMetricStore` composes the existing price, instrument,
corporate-action, financial-fact, entitlement, and quantitative services. It
does not create another source store or calculation ledger. Each report is
recorded through `QuantitativeStore.record_domain_calculation()` and returns
the shared calculation ID and hash. Replaying that receipt verifies the
canonical calculation input, output, revision IDs, and formula version.

## Price metrics

`calculate_price_metrics()` selects retained daily bars under both the
public-availability and acquisition cutoffs. Intraday and weekly bars are not
mixed into this calculation. Raw bars pass through the existing corporate
action service, which produces separate split-adjusted price and
cash-dividend-reinvested total-return closes. Provider-adjusted bars must use
one explicit `price_basis`; split-adjusted inputs can supply price returns,
while total-return-adjusted inputs can supply total returns. A mixed series,
zero close, unsupported basis, missing rights, duplicate session, or missing
required adjustment action fails closed. Provider-adjusted rows also need an
adjustment method, calculation reference, and adjustment cutoff no later than
the public cutoff; this prevents a present-day adjustment from silently
rewriting a historical view.

The price return and total return are end adjusted close divided by the first
adjusted close minus one. Drawdown is the lowest adjusted close divided by its
running peak minus one. Annualized volatility uses sample standard deviation
of observed interval returns multiplied by the square root of
`periods_per_year` (252 by default). Each successive observed daily bar counts
as one interval; the report exposes elapsed calendar days, does not synthesize
missing prices, and does not yet validate daily completeness against an
exchange-session calendar. Set `periods_per_year` explicitly for non-daily
series when this API is extended.

Benchmark correlation and beta use sample covariance and variance over an
exact inner join of the two return intervals' listing-local start and end
dates. No forward fill or zero-return substitution occurs. At least two paired
returns and nonzero variance are required. The two listings must have the same
currency because no FX path is applied to daily prices. Their current source
policies must permit derivation. The report includes input bar and action
revision IDs and the policy revisions authorizing derivation.

An optional `risk_free_rate_annual` is an effective annual rate. The Sharpe
calculation converts it geometrically to an observed period rate, subtracts it
from the arithmetic mean observed total return, then divides by sample return
standard deviation and annualizes by the square root of the configured period
count. The annual risk-free rate must be greater than -100 percent. The rate is
an explicit analyst input, not an automatically sourced yield series.

## Filed-fact metrics

`calculate_fact_metrics()` accepts requests for `growth`, `margin`, `leverage`,
`liquidity`, and `valuation_multiple`. Every input names an exact fact revision
ID selected at the requested cutoffs. Filing scale is applied to its exact
decimal lexical value. Ratios require matching period semantics and return a
dimensionless ratio. Growth compares the same canonical concept (or exact
taxonomy and tag) across comparable duration periods whose end dates are about
one year apart. Margins require matching duration periods; leverage and
liquidity require matching instant periods. A valuation multiple pairs
same-date instant facts or an instant numerator with a 350-to-366-day duration
ending no more than 366 days earlier. The service does not guess a canonical
tag or select facts on behalf of the analyst.

Zero and negative denominators return an `unavailable` metric with
`zero_denominator` or `negative_denominator`; no infinity, absolute-value
repair, or sign flip is applied. An unknown or incompatible unit is rejected.
When currencies differ, a request may supply an explicit FX rate with a
`rate_id`, `from`, `to`, positive `rate`, exact fact period, `rate_kind`, public
and observed timestamps, and source references. Instant facts require a spot
rate; duration facts require a period-average rate; each reference must be
public and acquired by the requested cutoffs and current rights must permit
derivation. Growth across different currencies is rejected until each period
can be converted with its own sourced rate. Currency conversion uses the
existing registered-unit service and emits its own quantitative receipt.

Each metric carries its formula version, unit, availability state, selected
fact revisions, and period. Filed-fact formulas are registered as immutable
`QuantitativeStore` metric revisions and evaluated through its safe formula
evaluator; the report carries both the formula revision and formula receipt.
The report adds all input source revisions, entitlement policy revisions, and
any unit-conversion calculation receipts. Domain formula IDs such as
`noesis-market-volatility-sample-v1` and `noesis-market-growth-yoy-v1` identify
the market convention; changing an estimator requires a new version.

## Limits and validation

Inputs are bounded to 20,000 daily price bars per listing and 200 requested
filed-fact metrics. A price range that reaches the bar limit is rejected so a
truncated range cannot look complete. Formula evidence to date is deterministic
synthetic-fixture validation; no live market provider, independent analyst
review, or production data coverage is claimed. REST/MCP calls and their
authorization, paging, and discovery behavior are documented in the
[market capability API guide](market-capabilities-api.md). The report contract
is `noesis-market-metric-report-v1`.
