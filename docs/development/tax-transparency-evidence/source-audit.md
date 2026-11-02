# Tax transparency: source-contract audit and bounded coverage (TX01)

Tracking: issue #2889 (`[Economics] Add tax transparency data`) · recorded
2026-11-02.

This audit sets out, per source, what an `economics.tax` provider in the
existing Economics bundle (`packs/economics/`) may acquire, how, and on what
terms. **It was written without live access to the publishers: the hosts
`sdmx.oecd.org`, `atlas-offshore.world` and `data.taxjustice.net` do not resolve
from this runtime (checked 2026-11-02), so no endpoint, dataflow id, column
name, code list or licence page was read live.** What is recorded comes from web
search results (OECD dataset pages, third-party catalogue mirrors of the OECD
Data Explorer, the Tax Justice Network's index pages and announcements) and from
the publishers' documentation as the author knows it. Every item marked
_verify_ must be checked against the live pages, the live terms and a real
response before the first dated live run. No source is `live` until that run
exists, and offline coverage is never reported as live coverage.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`DATASETS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and `LIVE_VERIFICATION` in
`src/ingestion/tax_sources.py` and must match this audit. The four acquired
sources ship in their own source pack, `economic-tax-transparency`
(`config/source_packs/economic-tax-transparency.json`, version 1.0.0), as the extractives
sources ship in `economic-extractives`; each carries the same
`live_verification` status.

Non-goals for every source: no own effective tax rate, profit-shifting or
tax-gap estimate; no ratio, share, per-capita or per-employee figure computed
from published values; no blending of OECD, EU Tax Observatory or company
figures; no ranking or risk score of jurisdictions or companies; no filled
jurisdictions or years; no currency conversion; no forecasts. Model-based
estimates keep their method and edition and are never presented as
observations.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `oecd-cts-cbcr` | OECD, Centre for Tax Policy and Administration: Corporate Tax Statistics database | anonymised and aggregated country-by-country reporting (CbCR) statistics: profit (loss) before income tax, income tax accrued and paid, employees, revenues and tangible assets, by ultimate parent jurisdiction and partner-jurisdiction grouping | OECD Data Explorer SDMX REST API (`https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}`), CSV output; agency `OECD.CTP.TPS`, structure `DSD_CBCR`, dataflows `DF_CBCRI` to `DF_CBCRVI` per third-party mirrors (_verify_ ids, version, dimension order and the `format=csvfilewithlabels` parameter) | `unverified-live` |
| `oecd-cts-rates` | OECD, Corporate Tax Statistics database | statutory corporate income tax rates (`OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT`, v2.0 per a mirror, _verify_) and forward-looking effective tax rates (`OECD.CTP.TPS,DSD_ETR@DF_ETR`, _verify_ the dataflow id) | same SDMX REST path | `unverified-live`; statutory rates are published legal parameters, forward-looking effective tax rates are **model estimates** of a hypothetical investment and are stored as such |
| `eutax-offshore-wealth` | EU Tax Observatory (Paris School of Economics): Atlas of the Offshore World | offshore financial wealth held by households of each country, as a model estimate (amount and share of GDP), per edition | "Download Data" on the Atlas site with a link to a GitHub repository (per search results; _verify_ the file, format, columns and the repository) | `unverified-live`; **licence not established** (no licence statement was visible in any result; _verify_). A run requires an operator to verify and accept the terms through the source-pack licence gate first |
| `eutax-billionaire-etr` | EU Tax Observatory: Global Tax Evasion Report 2024 (Alstadsæter, Godar, Nicolaides, Zucman; EU Tax Observatory Reports 004, released October 2023 per RePEc and eucrim) | billionaires' effective tax rates as model estimates per country, per report edition | the report PDF is hosted on `taxobservatory.eu`; a machine-readable data appendix was not found (_verify_ whether a CSV appendix or repository exists; the source is declared as a CSV on `www.taxobservatory.eu` and every path is _verify_) | `unverified-live`; **licence not established** (_verify_); same licence gate |
| Tax Justice Network: Corporate Tax Haven Index (CTHI) and Financial Secrecy Index (FSI) | Tax Justice Network | haven and secrecy scores and ranks per jurisdiction, per edition | TJN data portal (`data.taxjustice.net`), registration required; no documented machine API known (_verify_) | `not-implemented`: data are free only for non-commercial users after registration and commercial users must pay a licensing fee or subscription; inclusion in a product or service provided to third parties counts as commercial use (per the TJN country-profile and CTHI 2021 data-download pages found by search). Noesis cannot guarantee that every deployment is non-commercial, the portal requires a personal registration and automated access is not documented. Nothing is acquired or scraped; haven and secrecy index questions answer `not-implemented`, never empty |
| Public CbCR under Directive (EU) 2021/2101 | each in-scope multinational group (consolidated revenue above EUR 750 million in each of the last two consecutive financial years, _verify_) on its own website or in a business register | per-company income tax information reports, first for financial years starting on or after 22 June 2024 (_verify_), published within 12 months of the balance-sheet date and kept accessible for at least five years (_verify_) | no central register and no machine-readable collection known; Commission Implementing Regulation (EU) 2024/2952 sets a common template and machine-readable formats (_verify_ the number, date and the financial years it applies to) | **link-only reference records** (`reference-only`): an operator records the company name as published, the financial year, the report URL, the publication date and channel; no figure is stored. Company-level references reach ownership entities only through reviewable identity decisions. Revisit when a machine-readable source exists |

Candidates not audited here (each needs an amendment to this audit and a
source-pack version bump): the EU Tax Observatory's CbCR Explorer (a compilation
of published CbC reports, terms unknown), OECD Revenue Statistics (tax revenue by
type, already partly reachable through `economics.public-finance` and
Eurostat), and national tax-gap estimates.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: editions, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| `oecd-cts-cbcr` | `https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_CBCR@DF_CBCRI,{version}/{key}?startPeriod=...&endPeriod=...&format=csvfilewithlabels` (_verify_) | none | OECD Terms and Conditions; OECD data are stated to be available under CC BY 4.0 from 1 July 2024 (_verify_ on the live terms page) | OECD publishes per-IP request limits for the Data Explorer API (_verify_ the current figure); one request per declared document | the database is published as annual **editions** (Corporate Tax Statistics, one edition a year); each edition is a release vintage dated by its declared publication date; a value changed in a later edition is a new vintage, never an overwrite; a series a later complete edition no longer states becomes a `removed_by_source` vintage; confidentiality-suppressed cells are a status without a value; `OBS_STATUS` letters are kept verbatim (_verify_ the code list) |
| `oecd-cts-rates` | same path with `DSD_TAX_CIT@DF_CIT` and `DSD_ETR@DF_ETR` (_verify_) | none | as above (_verify_) | as above | as above; the forward-looking effective tax rate method (the OECD model and its parameters, _verify_ the methodology reference) is recorded per edition |
| `eutax-offshore-wealth` | Atlas of the Offshore World download (`https://atlas-offshore.world/...`, _verify_ the path and format) | none | not established (_verify_); redistribution not established | unknown (_verify_); one request per declared document | each Atlas update is an **edition**; a changed estimate in a later edition is a new vintage; the method (data sources and model, _verify_) is recorded per edition |
| `eutax-billionaire-etr` | Global Tax Evasion Report data (`https://www.taxobservatory.eu/...`, _verify_ that a machine-readable appendix exists) | none | not established (_verify_) | unknown (_verify_) | each report is an **edition** (2024 report, released October 2023); estimates of a later report are a new vintage |
| TJN CTHI / FSI | none acquired | - | non-commercial free after registration; commercial licence fee (per search results; full terms not read) | - | - |
| Public CbCR | none acquired; operator-declared report URLs | - | each report's own terms; only the link, name, financial year and dates are stored | - | a corrected report is a new revision of the reference; earlier revisions stay listed |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, a missing declared column, a response larger than the budget)
fails that source's run with its code and a receipt; earlier vintages stay
current and nothing is marked removed or revised because of a failure.
Readiness reports the source as `stale`. TJN index questions report
`not-implemented`, never empty.

## Definitions recorded per series

- **Nature.** Each dataset declares the nature of its values:
  `published_statistic` (statutory rates, CbCR aggregates as the OECD compiles
  them) or `model_estimate` (forward-looking effective tax rates, offshore
  wealth, billionaire effective tax rates). A `model_estimate` series carries its
  method (label, description and a citable reference) and every value carries the
  edition it comes from. Model estimates are never written into the Economics
  observation storage (`dataset_observations`), never labelled observations and
  never blended with published statistics.
- **Series key.** Provider, dataset, indicator, jurisdiction (code scheme and
  code), counterpart grouping (CbCR partner-jurisdiction grouping; otherwise
  `not_applicable`), unit with its multiplier, annual frequency and nature. An
  edition is a release, not part of the key, so each edition is a vintage of the
  same series.
- **CbCR caveats.** The OECD's own caveats travel with every CbCR series as a
  definition note (double counting of intra-group dividends and of income tax
  where a constituent entity is also a parent; differing accounting standards;
  confidentiality; _verify_ the wording per edition). CbCR aggregates describe
  groups of multinational groups, never a company.

## Data minimisation decision

The OECD and EU Tax Observatory sources publish aggregate statistics and
model estimates per jurisdiction; none returns data about a person or a single
company. Public CbCR reports are company-level, which is why they stay
reference-only. Decision:

- **Stored:** published aggregates and estimates per jurisdiction, period and
  series key, with status letters, notes, methods, editions and citations; for
  public CbCR, the company name as published, published identifiers (such as an
  LEI the report states), the financial year, the report URL, the publication
  date and channel.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** taxpayer-level or person-level data of any kind; the names of
  individual billionaires or wealth holders (the billionaire estimates are
  stored per country only); company-level figures from public CbCR reports (a
  reference never carries a profit, tax or employee figure); TJN scores, ranks
  and indicator values. A derived, filled, blended, ranked or forecast value is
  refused at write time.
- **Retention:** editions and vintages are kept for provenance; no erasure
  workflow applies.

## Bounded first coverage

| Source | Jurisdictions | Series | Periods | Caps |
| --- | --- | --- | --- | --- |
| `oecd-cts-cbcr` | ultimate parent jurisdictions Germany (`DEU`) and Netherlands (`NLD`) | `DF_CBCRI`: profit (loss) before income tax, income tax accrued, employees; partner groupings total, domestic and foreign (_verify_ codes) | from a declared start year, at most 5 years | 1 document, 40 series |
| `oecd-cts-rates` | Germany, Netherlands, Ireland | `DF_CIT` combined statutory CIT rate; `DF_ETR` forward-looking effective average tax rate (model estimate) | at most 5 years | 2 documents, 20 series each |
| `eutax-offshore-wealth` | Germany, Netherlands | offshore financial wealth (amount and share of GDP) | at most 5 years | 1 document, 20 series |
| `eutax-billionaire-etr` | France, Netherlands | billionaires' effective tax rate (share of wealth and of income; _verify_ the published measures) | the report's reference year | 1 document, 10 series |
| Public CbCR | operator-declared | link-only references | financial years starting on or after 2024-06-22 | 50 references per namespace and call |

Justification: two parent jurisdictions show the CbCR partner split and an
edition revision; three jurisdictions show statutory and effective rates side by
side with the effective rates kept as model estimates; two countries per EU Tax
Observatory dataset show an estimate revised between editions with its method.
Numeric values of published statistics reuse the Economics series storage
(`register_series` in `src/domains/economic/model.py`: `economic_vintages`,
`dataset_observations`) as the labour and tourism tracks do; model estimates
stay in the provider's own vintage tables with method and edition. Record shape
`statistical-series` (plus `versioned-documents` for the public CbCR
references); no new shape. Every further jurisdiction or dataset is a
source-pack version bump.

Taxonomy and depth: the closed subdomain list (ADR-005) has no tax subdomain, so
`economics.tax` names `public-finance` (revenue and tax), which is already
covered; it adds the OECD and the EU Tax Observatory as publishers behind it
(ADR-006). No gap row and no thin row changes.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| `oecd-cts-cbcr` | `unverified-live` | - | none yet; authored offline fixtures only |
| `oecd-cts-rates` | `unverified-live` | - | none yet; authored offline fixtures only |
| `eutax-offshore-wealth` | `unverified-live` | - | none yet; authored offline fixtures only; licence not established |
| `eutax-billionaire-etr` | `unverified-live` | - | none yet; authored offline fixtures only; licence not established; machine-readable appendix not confirmed |
| TJN CTHI and FSI | `not-implemented` | - | registration-gated portal, non-commercial-only free licence, commercial fee |
| Public CbCR | `reference-only` | - | no machine-readable source; operator-declared links only |

The fixtures are authored, not captured: synthetic values for fictional
reference years 2091-2093, so nothing can be mistaken for a published figure.
Edition release dates sit in 2024 and 2025 (past dates) because the source-pack
runtime retrieves at the real clock and the store refuses a release dated after
its retrieval (the convention the labour, business and tourism tracks settled).
Column names and codes in the fixtures follow the shapes described above and are
_verify_ throughout.
