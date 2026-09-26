# Market data provider evaluation

**Evaluated:** 2026-09-24  
**Scope:** initial U.S. public-equity release defined in [the market capability audit](market-analytics-capability-audit.md) and [roadmap issue #1647](https://github.com/Ikey168/Noesis/issues/1647)  
**Evaluation issue:** [#1655](https://github.com/Ikey168/Noesis/issues/1655)

## Delivery state (2026-09-26)

Audited against the repository on 2026-09-26 (C09.1, [Ikey168/Noesis#1840](https://github.com/Ikey168/Noesis/issues/1840)). Every item below cites a file or tool that exists in this checkout; where this section and older text disagree, this section is current.

- **Shipped:** the vendor shortlist and rights matrix (this document), the FMP adapter behind the provider-neutral EOD boundary (`src/ingestion/connectors/market/fmp.py`), the sample-evidence harness (`scripts/market_provider_sample_evidence.py`) and the hash-only live receipt (`config/market/acceptance_packs/live-price-provider.json`).
- **Partial:** the five-symbol entitlement sample (`live_incomplete`).
- **Not shipped:** a commercial quote and written retention, display and export rights; production enablement of any vendor (deliberately deferred).
- **Composition dependency of remaining work:** none (procurement and credentialed sampling).

## Recommendation

FMP is now the selected **technical adapter candidate**, but not an approved production provider. A credential-scoped live probe on 2026-09-24 covered all five identities and both NASDAQ/NYSE calendars. Its actual dataset scope was partial: the sampled key returned EOD prices, actions and statements for only two of the five issuers; the other three price/action requests and both delisted-price requests returned HTTP 402. Transcript and institutional-ownership requests returned 402 for all five. Commercial retention, display, derived-data and report-export rights remain unverified. The hash-only receipt is in `config/market/acceptance_packs/live-price-provider.json`; no key or raw payload is retained.

Do not enable production persistence or create a paid account from this document alone. Request written FMP Enterprise business-use terms and a full sample-data entitlement for Microsoft (`MSFT`), Oracle (`ORCL`), Salesforce (`CRM`), Adobe (`ADBE`) and ServiceNow (`NOW`). Ask FMP to confirm daily-price coverage, splits and dividends, delisted/recycled identities, filed and restated fundamentals, estimates, earnings transcripts, 13F/holder coverage, historical vintages, local storage duration, customer display, and exports of reports containing derived figures.

Shortlist these two commercial offers:

1. **Financial Modeling Prep (FMP) commercial Enterprise** is the broadest one-vendor candidate in its published catalog: prices, financial statements, estimates, earnings-call transcripts, Form 13F, calendars, company/filing data, and delisted-company endpoints. The commercial tier explicitly supports display and redistribution, but its price is quote-only. Ask for a contract that enumerates the planned local storage and customer-facing evidence/report workflow. The public $19/$49/$99 monthly prices belong to individual plans; they are not Noesis business-use prices.
2. **Twelve Data Venture or Enterprise** is a lower listed-cost candidate for a business-facing UI. The public business page lists external display, U.S. real-time data, global EOD equities, fundamentals, and other datasets at Venture. Enterprise lists external-distribution market data and a published monthly price. The provider's terms still require a separate agreement for redistribution, so neither tier should be treated as permission to export raw data or data-derived research without a written scope. Confirm per-endpoint access and rights.

Keep **Massive Stocks Business** as the reference option when consolidated U.S. market coverage, delisted history and corporate-action lineage matter more than cost. Its listed Stocks Business plan is $2,499/month and its Financials & Ratios business add-on is $699/month, before other data products; this puts the visible monthly baseline at $3,198. Its published catalog is stronger on market history than on the full research bundle: estimates, transcripts and ownership appear to need separate products or agreements.

Use **Tiingo Power** only as a lower-cost benchmark for private internal research, not as the default provider for a Noesis service that displays data to other people. Tiingo lists commercial/internal access at $50/month or $499/year, but defines API data as internal-only; redistribution requires permission and additional fees. Fundamentals are a separately priced add-on. Do not expose or export the data based only on this tier.

Keep **SEC EDGAR and the existing macro sources** as primary evidence inputs regardless of price-vendor choice. They do not replace a licensed market-price feed. If a paid source is down, label affected values stale or missing and use filing/macro evidence only where relevant; do not silently substitute a different price source.

## Comparison

Plan prices below are vendor-published list prices observed on the evaluation date. They are not negotiated quotes, invoices, or a legal interpretation of each agreement. Vendor coverage and freshness claims are not independently verified by this evaluation.

| Provider / commercial tier | Daily prices, actions, history and delistings | Fundamentals, estimates, transcripts and holders | Price and limits | Rights, storage and export | Assessment |
|---|---|---|---|---|---|
| **[FMP commercial Enterprise](https://site.financialmodelingprep.com/pricing-plans?planType=commercial)** | Catalog includes real-time market data, historical EOD prices, corporate actions/calendars, historical coverage advertised as 30+ years, delisted-company records, and survivorship-bias-free history. Actual dates and the five-symbol sample need a quote/sample check. | Its commercial catalogue lists financial statements, analyst estimates, earnings-call transcripts, institutional Form 13F and related data. Form 13F is a lagged institutional filing view; do not present it as complete beneficial ownership. | Price is **contact sales**. Commercial page advertises 3,000+ calls/min and 99.9% SLA; verify endpoint-specific limits, bandwidth, support and SLA in the order form. | Enterprise is described as display and redistribution. [FMP terms](https://site.financialmodelingprep.com/terms-of-service) restrict third-party access and derived data without the commercial grant; obtain explicit rights for API display, offline report export, derived metrics, retention and source citations. | Best breadth for a single commercial quote. Highest uncertainty until price, symbol-level coverage, historical revisions and report-export terms are received. |
| **[Twelve Data business Venture / Enterprise](https://twelvedata.com/pricing-business)** | Business pricing lists U.S. real-time, global EOD equities and corporate-action endpoints including splits. Its public page lists 70+ markets at Venture; per-symbol history depth and complete event coverage must be checked. | The pricing catalog lists statements, direct/institutional holders, earnings estimates, estimates/revisions and ratings. Earnings-call transcripts were not confirmed in the reviewed business catalog. Confirm each endpoint's tier and each of the five sample issuers. | Venture's displayed card is $499/month or $4,990/year, with configurable credits; the footer advertises Venture from $149/month. Enterprise is shown at $1,099/month or $10,992/year. API credits are endpoint-weighted, so estimate actual monthly call cost from the proposed refresh schedule. | Business tiers permit specified commercial/external display subject to exchange terms. [Twelve Data's usage policy](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage) says redistribution requires a separate agreement. Public export, retention, cached source payloads and derived report figures need explicit written permission. | Cost-effective shortlist for dashboards. Terms, external distribution, endpoint entitlements and usable daily history require direct confirmation. |
| **[Massive Stocks Business + Financials & Ratios](https://massive.com/business)** | Business page advertises U.S. coverage, 20+ years, reference data and corporate actions. [Stocks coverage information](https://massive.com/stocks) describes active/delisted history and deep market data. The business plan states no exchange fees/approvals for the included feed; verify exactly which exchanges, bars, adjustments, events and redistribution uses are in scope. | Business financials add-on lists income statement, balance sheet, cash flow and ratios, with a separate $699/month list price. Estimates, transcripts and ownership are not in the base bundle assessed here; partner datasets or separate licenses would add cost. | $2,499/month Stocks Business plus $699/month Financials & Ratios = **$3,198/month** list baseline. Business API calls are advertised as unlimited. Enterprise SLA is custom; no measured availability or correction service was checked. | Individual Stocks tiers are nonprofessional/personal. Business is the relevant commercial tier; get written confirmation for Noesis's user count, on-prem/local retention, cached prices, charts, citations and exported reports. | Strong price/history and identity benchmark, but expensive for a five-company EOD pilot and incomplete as one-source research coverage. |
| **[Tiingo Power (commercial/internal)](https://www.tiingo.com/about/pricing)** | Vendor lists 15+ years in its commercial plan and [EOD documentation](https://www.tiingo.com/documentation/end-of-day) describes historical raw and adjusted prices, split/dividend handling, and corrections during the evening. A stable permaTicker is documented for delisted/recycled symbols. Check the five-company start dates and event histories in a sample. | [Fundamental statements](https://www.tiingo.com/documentation/fundamentals) are available as a third-party add-on (quoted separately); the public page describes more than 20 years of U.S. equity fundamentals for 5,500+ equities and an update cycle usually 12–24 hours after SEC availability. Estimates, call transcripts and holder data were not confirmed as included. | $50/month or $499/year internal commercial plan, with published request/bandwidth limits. Fundamental add-on and any redistribution are not included in that cost. | [API terms](https://api.tiingo.com/tos/) say data is internal-only; businesses need a commercial plan and redistribution needs special permission and additional fees. Explicitly insufficient for general customer display or export without that permission. | Lowest published commercial internal price in this shortlist; useful for a restricted internal pilot if its rights match actual use. |
| **[SEC EDGAR](https://www.sec.gov/about/developer-resources) (existing Noesis connector)** | Not a quote source. Supplies filings/submissions and XBRL facts. | Primary filed values and documents, not consensus estimates, transcripts as a normalized product, or holder-wide data. Existing Noesis extraction maps five concepts and uses the latest filed fact for each concept/period. | SEC API access is free; fair-access requirements apply. The connector requires a descriptive `NOESIS_EDGAR_USER_AGENT`. | Follow SEC access requirements and retain accession/source provenance. SEC filings remain the primary check for vendor-normalized fundamentals. | Keep as a source-of-record cross-check and evidence source, not a market-data vendor. |

### Sources reviewed

- [FMP commercial pricing and dataset catalogue](https://site.financialmodelingprep.com/pricing-plans?planType=commercial) and [FMP terms](https://site.financialmodelingprep.com/terms-of-service).
- [Twelve Data business pricing](https://twelvedata.com/pricing-business), [commercial/personal use policy](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage), and [API documentation](https://twelvedata.com/docs).
- [Massive business pricing](https://massive.com/business), [stock coverage and business rights overview](https://massive.com/stocks), and [stock API documentation](https://massive.com/docs/rest/stocks/overview).
- [Tiingo pricing](https://www.tiingo.com/about/pricing), [EOD API documentation](https://www.tiingo.com/documentation/end-of-day), [fundamentals documentation](https://www.tiingo.com/documentation/fundamentals), and [API terms](https://api.tiingo.com/tos/).
- [SEC developer resources](https://www.sec.gov/about/developer-resources).

## Rights and operations matrix

These are the questions the signed order or license must answer before configuration is enabled. No right should be inferred from an API plan name, sample response, or website pricing card.

| Use | Required confirmation |
|---|---|
| Local ingestion and persistent history | Whether Noesis may retain raw responses, normalized bars, filings/derived ratios, and snapshots; retention/deletion periods; data-residency and backup rules. |
| Internal dashboard | Authorized-user definition, display rules, user counts, professional subscriber declarations, exchange fees, and whether local-first/on-prem use is permitted. |
| External dashboard / API | Explicit customer-facing display and any redistribution sublicense, attribution, non-display analysis, derived-data reconstruction thresholds, and per-exchange approval/fees. |
| Research report / evidence bundle | Whether report values, excerpts, charts, attachments and source locators may leave the licensed workspace; define treatment of uncited vendor metrics and offline exports. |
| Cached fallback and outage recovery | Permitted cache duration; grace period on contract lapse; correction/backfill rights; source failover permission; incident and data-removal procedure. |
| Historical research | Availability of point-in-time/as-filed company financials, old symbols and delistings, corporate-action revisions, event timestamps and vintage history. If absent, Noesis must record the limitation and must not imply as-known-then fidelity. |

## Live sample result and remaining validation

The supplied FMP credential was probed without retaining raw responses. Profiles resolved all five target tickers. The hash receipt records two active symbols with usable oldest/recent EOD windows and three with price requests rejected as not entitled; two provider-returned delisted U.S. identities were found but their price requests were also rejected. No duplicate bars or recent missing sessions appeared in the two available samples. No provider correction was observed during this point-in-time run, so deterministic correction fixtures remain the only correction evidence. The run is `live_incomplete`, not a production approval.

Use `scripts/market_provider_sample_evidence.py` with a copy of
`config/market/provider-sample-manifest-template.json` for each authorized
sample. The harness records request/response hashes, explicit coverage and
rights findings, history/calendar/correction checks, and delisted/recycled
identity evidence without retaining credentials or payloads. A fixture run is
always labeled `fixture_verified_only`; missing access is
`credential_blocked`; and `live_evaluated` means only that the authorized
sample was assessed, not that procurement or production use was approved.

For every requested symbol, reconcile at least these cases:

1. Latest daily OHLCV, venue/session time, currency and provider publication time; compare official close timing and missing-session behavior.
2. Ten-year boundary, IPO/first covered date, ticker changes, split and dividend events, and at least one correction/restatement. Keep raw and adjusted series separate and document the vendor adjustment formula.
3. The five target companies' annual and quarterly income, balance sheet and cash-flow records, their accession/period/publication metadata, and a sample of historical values as initially filed versus subsequently revised.
4. Analyst-estimate fields and history, earnings transcripts and dates, direct/institutional holder identities and report periods; establish whether “ownership” means SEC 13F only, insider transactions, or another coverage class.
5. API weights/limits for the proposed backfill and daily refresh, rate-limit behavior, outage/correction notices, source revision identifiers, and read-after-write consistency.

Reject or qualify a provider if it cannot identify the source/publication time, distinguish original versus adjusted prices, document corrections, or grant the retention/display/report rights Noesis actually needs. Repeat the coverage sample before contract renewal or a material plan/terms change.

## Costed recommendation by deployment mode

1. **Private internal analyst pilot:** quote Tiingo Power and Twelve Data business/internal options; the Tiingo public commercial/internal list price is $50/month or $499/year before fundamentals. Keep data inside the authorized workspace and avoid sharing provider values in reports until report rights are in writing. EDGAR remains the no-cost filings cross-check. This is a cost benchmark, not a vendor selection.
2. **Noesis product with customer-facing company/market dashboards:** request commercial quotes for Twelve Data Venture/Enterprise and FMP Enterprise. The public reference points are Twelve Data's Venture card at $499/month ($4,990/year) and Enterprise at $1,099/month ($10,992/year); FMP is quote-only. The vendor must state whether the price includes U.S. exchange reporting, all users, external display, derived metrics, and report exports. If it does not, compare the separate redistribution/exchange fees before deciding.
3. **Broad U.S. price-feed / research depth:** request a Massive quote including the $2,499/month Stocks Business tier and $699/month Financials & Ratios add-on as a public list baseline. Add quotes for estimates, transcripts and ownership only if those datasets are part of the agreed release. This is a comparison ceiling for the first five-company EOD workflow, not a recommended spend.
4. **Intraday:** defer the purchase. Existing plan cards show real-time products, but Noesis's approved first release targets end-of-day data. Add an intraday feed only after phase 5 establishes a user need, latency target, exchange-level rights and measured operations cost.

## Decisions and follow-up

- **Shortlist:** FMP Enterprise and Twelve Data business for breadth/external display; Massive Business for price-depth benchmarking; Tiingo Power for an internal-only cost benchmark.
- **Technical selection:** FMP, implemented behind the provider-neutral EOD boundary. This does not select or purchase a commercial plan.
- **Do not enable yet:** a suitable FMP plan, business quote, five-symbol/delisted entitlement and legal terms for retention, display, derived values and Noesis report exports have not been confirmed.
- **Fallback:** current SEC EDGAR and configured public macro-statistics paths. They cannot be presented as substitute equity prices. Missing price values remain unavailable or stale.
- **Next build:** use the provider-neutral contracts in [#1656](https://github.com/Ikey168/Noesis/issues/1656), carry entitlement metadata with each source series, and run the representative sample before enabling a vendor in production.

## Scope and limitation

This is a desk evaluation of vendor-published product, pricing and usage pages as of 2026-09-24. It is not legal advice, a negotiation, an API payload comparison, a quote, an invoice or a recommendation to trade securities. Vendor documentation and prices can change; refresh them before procurement.
