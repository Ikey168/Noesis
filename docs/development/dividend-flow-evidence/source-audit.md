# Dividend-flow estimates: source audit (#2891)

This audit decides which declared-dividend sources feed the derived
dividend-flow estimates
([method note](../../subsystems/dividend-flow-estimates.md)). No source was
acquired for this issue. The method reuses records that the Market pack and
Corporate Ownership already hold. The result is also kept in machine-readable
form as `DIVIDEND_SOURCES` in `src/domains/market/dividend_flows.py`, which is
returned with every answer.

| Source | Decision | Reason |
|---|---|---|
| Market corporate actions (`noesis-market-corporate-action-v1`, `cash_dividend`) | **implemented** (reused) | Already revisioned, with ex, record and payable dates, currency, amount basis, distribution type and provider entitlement. Each input is checked against the `derive` capability of its entitlement before it is used. |
| SEC CompanyFacts per-share dividends (`us-gaap:CommonStockDividendsPerShareDeclared`, `ifrs-full:DividendsRecognisedAsDistributionsToOwnersPerShare`) | **implemented** (reused, fallback) | Indexed by the existing CompanyFacts path (`companyfacts_to_market_facts` keeps all `us-gaap` and `ifrs-full` concepts). The facts carry no record date, so the whole period counts as the timing gap. They are used only when an issuer has no corporate-action dividend in the window, to avoid double counting. |
| SEC CompanyFacts shares outstanding (`us-gaap:CommonStockSharesOutstanding`, `ifrs-full:NumberOfSharesOutstanding`) | **implemented** (reused) | Converts a stated percentage into shares. The gap between the fact date and the holding date is flagged. |
| SEC CompanyFacts dividend payments (`PaymentsOfDividendsCommonStock`, `PaymentsOfDividends`) | **context only** | These are company-wide cash totals and can include preferred and non-controlling dividends. They are shown as reported context and never attributed to a holder. |
| `dei:EntityCommonStockSharesOutstanding` | **not-implemented** | The CompanyFacts market-fact path skips the `dei` cover taxonomy (`src/ingestion/connectors/edgar.py`). Indexing it would change that connector's scope and is outside this issue. |
| Bundesanzeiger (annual accounts, dividend resolutions of German issuers) | **not-implemented** | No permitted machine access. The Bundesanzeiger publishes documents behind an interactive search, with no API and no bulk export, and its terms of use do not permit automated retrieval. The repo's only Bundesanzeiger source, the net-short-position CSV under the BaFin feature, is a separate publication and remains `unverified-live`. German issuers' dividends enter only as Market corporate actions from an entitled provider. |

## Holdings used

The method uses only holdings that the ownership graph already records, each
under its own disclosure rule:

- SEC 13F information tables and Schedule 13D/13G cover pages
  (`docs/development/ownership-evidence/sec-holdings-source-audit.md`);
- BaFin WpHG § 33 voting-rights notifications, projected as `voting_rights`
  assertions;
- UK PSC and ROE share bands, and BODS statements
  (`docs/development/ownership-register-audit.md`).

Holdings below a regime's publication threshold are never estimated.

## Evidence

The evidence is offline only: the synthetic fixture
`tests/unit/dividend_flow_harness.py` with the tests in
`tests/unit/domains/test_dividend_flows.py` and
`tests/unit/domains/test_dividend_flows_mcp.py`. No live sample was taken, and
no estimate has been compared with a holder's own reported dividend income.
