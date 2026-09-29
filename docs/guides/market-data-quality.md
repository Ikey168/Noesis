# Market data quality, review and repair

`MarketQualityStore` produces immutable, point-in-time assessment receipts over the existing market source stores. It stores revision/hash references and findings; bars, actions and filing facts remain in their authoritative stores. A quality assessment is scoped to a namespace and principal, includes explicit public and acquisition cutoffs, and can be replayed only while its input state is unchanged.

## Price range assessment

Call `assess_price_range()` with a listing, interval, half-open millisecond range, both cutoffs and a freshness threshold. Optional thresholds set the relative tolerance for provider disagreement and the close-to-close move that should be reviewed. Findings can identify missing history, a store row limit, duplicate provider bar identities, missing or inconsistent OHLC values, nonpositive closes, a mismatch with the listing currency, unusually large moves, differences between providers, unknown/future publication or retrieval clocks, stale history, and quarantined source revisions.

Coverage remains `unverified` unless a retained trading-session calendar covers the requested dates. A supplied calendar produces separate counts for expected sessions, observed bars, no-trade sessions, missing bars and unknown sessions. This avoids treating exchange holidays as data loss or claiming complete history from a price query alone.

## Filing fact assessment

Call `assess_financial_facts()` with an issuer, point-in-time cutoffs and a freshness threshold. Optional taxonomy, concept and filing-form filters narrow the selected facts. The assessment reports missing or truncated results, stale or future retrieval times, facts without an accepted canonical-concept mapping, unit differences for otherwise comparable facts, and differing values reported by separate filings for a matching concept, period and statement. Filing differences are review information; they do not by themselves prove that one filing is erroneous or that a value is an amendment.

## Quarantine and review

`quarantine_revision()` and `release_quarantine()` record reviewer, reason, finding IDs and timestamps in an append-only event history. Decisions apply to one immutable source revision. Price, corporate-action and financial-fact reads omit currently quarantined revisions after selecting the revision valid for the requested cutoff; releasing a quarantine makes the retained revision eligible again. Repeating the same decision is idempotent and does not add an audit event. These operations require the market quality review scope and namespace access; non-operators also need access to the source revision.

During price ingestion, a normalized row with a supported data-level contract error (such as a currency mismatch) is skipped while valid rows in the same page continue. A candidate receipt retains a digest, a safe provider record identifier when available, and an error code; it does not retain the rejected payload. Provider outage, malformed page envelope and other run-level failures use the bounded ingestion checkpoint and failed/paused status rather than being relabeled as bad individual records.

Price-history page responses report authorized revisions excluded by active quarantine without returning their payloads. Company dashboards carry the exclusion count and mark the panel quality `degraded`; market metric receipts do the same while pinning only the revisions actually used in the calculation. This prevents a quarantined row from disappearing silently or being mistaken for a complete calculation input. Entitlement checks happen before an exclusion identifier or reason is exposed.

## Bounded repair

`create_price_repair_plan()` records a provider, listing, interval, date range and bounded `MarketIngestBudget`; it does not store credentials or source payloads. `run_price_repair_plan()` delegates to `MarketPriceIngestor`, whose checkpoint supports safe resume and whose price upserts are idempotent. A repeated completed run returns the stored result. The selected provider callback remains responsible for credentials, provider-specific parsing and source terms.

Run focused checks with:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/unit/domains/test_market_quality.py tests/unit/domains/test_market_prices.py tests/unit/domains/test_market_actions.py tests/unit/domains/test_market_financial_facts.py
```

These checks use deterministic fault injection for outages, malformed payloads,
partial retries and corrections. The FMP acceptance pack separately records
the supplied credential's real HTTP 400/402 endpoint failures; those failures
are evidence of partial access, not a simulated successful provider. Company
dashboards and market metric receipts expose selected revisions and authorized
quarantine exclusions. Commercial provider scope and rights remain tracked in
[market issue #1655](https://github.com/Ikey168/Noesis/issues/1655).
