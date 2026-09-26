# EDGAR financial facts

The legacy `facts_to_filing_facts()` path remains available to the existing filing note and five-metric `ObservationStore` workflow. The broader path is `harvest_market_financial_facts()` in `src/ingestion/connectors/edgar.py`, connected to `MarketFinancialFactStore` through `FilingsConnector.ingest_market_financial_facts()`.

Resolve the filer to a Noesis `issuer_id` through the instrument master, configure `NOESIS_EDGAR_USER_AGENT`, then provide that identity, namespace and current access context to the connector. The connector reads company facts plus the submissions feed, maps facts into `noesis-market-financial-fact-v1`, and passes the batch to the store. It does not infer a stable issuer ID from a current ticker.

## Preserved filing semantics

Each source fact retains its accession, filing form, taxonomy tag, XBRL unit, exact numeric lexical value, fiscal year/period, filed/accepted/public/retrieved clocks, actual period start/end dates, and a source locator. Values are never converted to binary floating point in this path. CompanyFacts does not expose native `contextRef`; `context_id_kind` is therefore `companyfacts_composite_key`, and the context key includes accession, tag, unit, period, fiscal labels and frame when present. `source_document_revision_id` points to the SEC filing accession; the existing narrative-note connector is not claimed to be the filing body itself.

Duration classification uses the actual reported dates: short 70–120 day periods are marked `quarter`, Q2/Q3 durations longer than a quarter are `year_to_date`, and 330–400 day periods are `annual`. This preserves 52/53-week fiscal-year boundaries. YTD values remain separate from standalone-quarter values; this path does not derive Q2/Q3 or Q4 values by subtraction. Instant facts such as assets use a separate instant period object.

As-filed accession and context are part of the stable observation identity. A 10-K/A therefore creates distinct facts alongside the original 10-K. A changed value for the same accession/context appends a local revision. `latest_facts_by_period()` selects the latest accessible filing for each exact taxonomy tag, unit and actual date period under explicit acquisition/public cutoffs; contexts in the chosen filing remain separate. Same-filing conflicts are returned with `partial` readiness and a diagnostic rather than silently collapsed.

The normalizer also reports missing core concepts, unknown public time, invalid filing metadata and unmapped tags. `canonical_concept` is a small versioned alias map for common statement facts; exact taxonomy tags remain authoritative, and unsupported concepts carry `mapping_status="unmapped"`. `statement="other"` means statement placement is not currently mapped.

## Filing reconciliation boundary

`reconcile_market_facts_to_filing()` compares a normalized batch with facts
extracted from an actual filing or Inline-XBRL instance. The filing adapter
supplies accession, taxonomy tag, unit, actual period and exact numeric value;
matching intentionally ignores the context identifier so CompanyFacts' lack of
native `contextRef` does not create false mismatches. The result reports
matched, missing, extra and value-mismatched rows, conflicting normalized
contexts, invalid filing rows and unsupported accounting mappings. It never
chooses between conflicting contexts or derives a missing value. This keeps an
instance-document parser and its native-context evidence as a replaceable
boundary around the existing CompanyFacts index.

## Coverage limit

The SEC's [CompanyFacts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) provides standard taxonomy facts by concept and unit. It does not supply all custom or dimensional facts, and its separate frame API aligns facts to approximate calendar periods even when company fiscal calendars vary. This implementation does not claim native filing context identity or full statement presentation for those omitted facts. The exact filing URL, accession and composite source locator are preserved so a later instance-document/Inline XBRL parser can add native contexts without replacing these observations.

Read requests require `market:financial-facts:read`, namespace access and the current source-specific entitlement. Write requests require `market:financial-facts:write`, namespace write access and ingestion entitlement. Facts with unknown public time can be stored for provenance but are excluded from any query with a public-time cutoff. When submissions metadata has no accepted timestamp, the fallback public time is the end of the filed UTC date, which avoids claiming availability earlier in that day.

Fixture coverage checks quarter versus YTD, instant versus duration facts, a 53-week annual period, amendment preservation, lexical values, acceptance times, conflicts, missing metadata, unmapped concepts, filing reconciliation and batch indexing:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
  tests/unit/ingestion/connectors/test_edgar_market_facts.py \
  tests/unit/domains/test_market_financial_facts.py
```

## Live SEC reconciliation evidence

`scripts/market_live_sec_evidence.py statements` reconciles the latest 10-K and
10-Q of each issuer in the representative universe: SEC CompanyFacts values are
compared with the native-context Inline XBRL facts of the same accession. The
receipt stores accessions, locators, counts and diagnostics only; no filing
payload is retained. The run recorded in
`config/market/acceptance_packs/live-sec-statements.json` (2026-09-24) covered
MSFT, ORCL, CRM, ADBE and NOW: 10 filings, 4,897 comparable facts, 4,897
matched, no mismatched, missing or conflicting values.

Comparison rules, all derived from live filings:

- Values are compared numerically (`13.7` equals `13.70`); lexical forms are
  kept in diagnostics.
- Transformation Registry 3+ names (`ixt:num-dot-decimal`, `ixt:fixed-zero`)
  and SEC `ixt-sec:numwordsen` are parsed; other transforms stay diagnosed.
- A fact tagged twice at different precision (for example "$22.8 billion" in
  prose beside the exact table value) is a *consistent duplicate* when the less
  precise value lies within its `decimals` rounding interval; the precise value
  is compared. Inconsistent duplicates remain `conflicting_contexts`.
- A zero-length duration in the filing and CompanyFacts' instant on the same
  date are the same period.
- Dimensional (segment/member) facts and filer extension taxonomies are outside
  CompanyFacts coverage; the receipt counts them under `coverage` instead of
  reporting them as missing.

Each receipt has `value_status` (`consistent` or `review_required`) separate
from `readiness`, which stays `partial` while unmapped accounting tags exist.
This evidence validates SEC-to-SEC normalization; it does not validate a
commercial fundamentals vendor or record an analyst review. SEC requests need
a descriptive `NOESIS_EDGAR_USER_AGENT`; SEC rejects some agent strings with
HTTP 403, which the connector reports as an error rather than empty data.
