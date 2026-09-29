# Market corporate actions and adjustment rules

`MarketCorporateActionStore` in `src/domains/market/actions.py` keeps action observations as immutable, namespaced revisions. Each action links to stable issuer/security/listing IDs, keeps announced/effective/ex/record/payable dates distinct, and cites provider revision and entitlement evidence. A correction appends a revision; a cancellation does not erase the earlier observation. Reads choose the latest revision both acquired and publicly available by the caller's cutoffs.

The action vocabulary captures forward and reverse splits, cash and stock dividends, spin-offs, mergers, delistings and symbol changes. Share ratios mean new or distributed shares per old share. Cash dividend records state the quoted per-share basis and distribution class (`regular`, `special`, `return_of_capital` or `liquidating`). Related-security IDs connect merger and spin-off events when that identity is known.

## Current calculation boundary

`calculate_adjusted_series()` accepts only raw daily bars for one listing and currency. It maps each UTC bar start to the listing's IANA venue timezone, then joins events on the local session date. A bar that is already split-adjusted or total-return-adjusted is rejected, which prevents double adjustment. Every action ex-date in the requested span must have an eligible daily bar; the service refuses to reinvest a dividend at a later observed close when its actual ex-date bar is missing.

For an action with ratio `r = new shares / old shares`, the split-neutral price return over a period is:

```text
(price at end × product of split ratios in the period) / price at start
```

The backwards split factor for a bar is the product of `1/r` for split events after that bar through the last included session. Raw price observations remain unchanged. Both the price-return index and split-adjusted close are returned alongside the raw close and factor.

The total-return index includes cash dividends and assumes the dividend is reinvested at the ex-date close. Splits are applied first; the dividend share basis determines whether cash is multiplied by pre-action or post-action shares. Taxes, withholding, fees, fractional-share restrictions and foreign-exchange conversion are excluded. The total-return adjusted close is rebased so its final value equals the last raw close. Results are rounded half-even to 12 decimal places; source prices and action amounts remain in their original records.

The formula version and exact bar/action revision IDs are recorded through the existing `QuantitativeStore` calculation receipt (`noesis-quantitative-calculation-v1`). The market-domain result contract also retains versioned factors, public/acquisition cutoffs and source entitlement IDs, so replay uses the same inputs and current read authorization can be checked.

## Explicitly unsupported cases

Spin-offs, mergers, stock dividends, delisting proceeds and other non-cash/share-exchange actions are retained but cause a calculation crossing the event date to fail with `unsupported_action`. They need an independently validated successor-asset valuation and distribution rule before a continuous return can be stated. Identical cross-provider observations are applied once while preserving every source revision ID. Providers that disagree on a split ratio or same-class distribution amount cause `conflicting_actions`; the service does not sum conflicting revisions as if they were separate events.

Only `confirmed` or `corrected` actions affect the calculation. Announced, unknown and cancelled records remain inspectable but have no return effect. A public cutoff excludes later corrections, and an acquisition cutoff excludes revisions learned later. This provides an as-known-then reconstruction only where bars and action publication timestamps are known; missing historical vintages remain missing.

Validation uses synthetic fixtures and hand-computable cases for 2:1 and 1:10 splits, cash dividends, corrections, duplicate/conflicting vendors, unsupported actions and double-adjusted bars:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/unit/domains/test_market_actions.py
```

No authorized live vendor action sample has been evaluated. Provider-specific event completeness, adjustment conventions and corrections remain gated on #1655 and #1658.
