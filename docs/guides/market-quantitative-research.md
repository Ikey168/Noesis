# Reproducible quantitative market research

The market quantitative surface stores immutable input manifests and result
receipts for six bounded calculations:

- event studies align events to retained sessions, estimate benchmark-adjusted
  abnormal returns, expose overlapping events/confounders, and apply a
  Bonferroni family correction;
- factor analysis fits dated market/size/value/momentum/quality or
  user-defined factors, reports missingness and collinearity, and can hold out
  a dated sample;
- backtests require execution after the signal, retain strategy versions,
  universe/source references, missing exits, commissions, slippage, borrowing,
  turnover, and cost sensitivity;
- walk-forward evaluation enforces feature availability, train/test windows,
  temporal gaps, leakage checks, naive baselines, and held-out errors;
- portfolio revisions reconcile dated transactions, holdings, cash, fees, FX,
  time-weighted and money-weighted returns, and benchmark-relative results;
- risk reports calculate dated sector/geography/currency/factor exposures,
  concentration, drawdown, liquidity, historical VaR/expected shortfall, and
  explicit scenario contributions.

REST and MCP adapters call the same capability service. Every persisted run can
be inspected or exported with its formula version, record hash, and replay
manifest. Sparse data, delistings, missing prices, collinear factors, look-ahead
leaks, and incomplete liquidity mappings remain visible or fail with typed
errors. Statistical estimates are descriptive; they are not causal proof or a
guaranteed risk bound.
