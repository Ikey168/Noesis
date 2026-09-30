# Insurance supervisory statistics, SFCR, NAIC and catastrophe losses: source and licence audit (IN01, #2557)

Tracking issue: #2230. This audit records, for each candidate source and
publisher, the access path, the format, the identifiers it publishes, the
licence and attribution terms, the rate limits, how revisions appear, and the
access or licence decision that the Market pack's optional `insurance` feature
implements. It also fixes the bounded insurers, markets, events and periods
that Noesis acquires.

**Verification status.** No live request was made while writing this audit,
because the build environment has no network access to the publishers. Every
claim marked **(verify)** comes from the publishers' public documentation and
portal behaviour as known when this was written. Each one must be checked
against the live terms and services before live acquisition is accepted
(IN13, #2569). The adapters in `src/ingestion/insurance_sources.py` parse the
documented shapes through **declared column and cell mappings**. A changed
header or template layout is therefore a declaration change, not a code
change. The offline fixtures under `tests/fixtures/insurance/` are authored and
name fictional insurers and fictional events. None of them is captured data.

`PROVIDER_CONTRACTS`, `LICENCE_DECISIONS` and `LIVE_VERIFICATION` in
`src/ingestion/insurance_sources.py` restate these decisions in code. Every
implemented source is `unverified-live` until a dated live run is recorded
under this directory.

**Exclusions (tracker).** Noesis makes no solvency or rating assessment,
builds no loss model, and computes no ratio, reconciliation, projection or
aggregate across publishers. Figures are stored and answered exactly as
published.

## Summary of decisions

| Source / publisher | Access path | Format parsed | Identifiers | Decision |
| --- | --- | --- | --- | --- |
| EIOPA insurance statistics | `https://www.eiopa.europa.eu/tools-and-data/insurance-statistics_en` (download links per release) | XLSX workbook (`eiopa-statistics-xlsx`) or a flat CSV export (`eiopa-statistics-csv`) through a declared column mapping | EIOPA country (ISO 3166-1 alpha-2, `EEA` totals), line of business as published, reference date, dataset name | `in-scope` (unverified-live) |
| Insurers' SFCR reports (Solvency II Art. 51, public QRTs of Implementing Regulation (EU) 2023/895) | the insurer's investor-relations or regulatory-disclosure page | PDF (`sfcr-pdf`), text through the existing PDF extraction path (`src/ingestion/connectors/paper/pdf_parser.py`) | LEI as stated in the report, group or solo reporting level, reporting year | `in-scope` (unverified-live) for the named sample |
| NAIC public market share reports | `https://content.naic.org/` (research and publications) | PDF | NAIC company code, NAIC group code, reporting year | `metadata-only` |
| NAIC licensed data products (Financial Data Repository, iSite+, annual statement data, InsData) | subscription | n/a | n/a | `excluded` (licensed or subscription-only) |
| Florida Office of Insurance Regulation (OIR) catastrophe claims data reports | `https://floir.com/` (hurricane and catastrophe claims data pages) | CSV (`catastrophe-estimates-csv`) through a declared column mapping | event name as published, NHC storm name, report date | `in-scope` (unverified-live) |
| NOAA NCEI U.S. Billion-Dollar Weather and Climate Disasters | `https://www.ncei.noaa.gov/access/billions/` | CSV (`catastrophe-estimates-csv`) | event name, begin and end dates, CPI-adjusted estimate vintage | `in-scope` (unverified-live); an archive, since NCEI stopped updating the product in 2025 **(verify)** |
| PERILS AG industry loss estimates | `https://www.perils.org/` | n/a | PERILS event name | `excluded`: the loss index is a licensed product; the press releases are copyrighted and restate the licensed data |
| Verisk Property Claim Services (PCS) | subscription | n/a | PCS catastrophe serial number | `excluded` (licensed) |
| Swiss Re Institute sigma | `https://www.swissre.com/institute/research/sigma-research.html` | PDF | event name | `metadata-only`: reproduction of figures needs permission **(verify)** |
| Munich Re NatCatSERVICE | `https://www.munichre.com/en/solutions/for-industry-clients/natcatservice.html` | web tables, PDF | event name | `metadata-only`: terms restrict reuse of the database **(verify)** |
| Insurance Council of Australia catastrophe list | `https://insurancecouncil.com.au/` | XLSX | ICA catastrophe number | `metadata-only` until the reuse terms are confirmed **(verify)** |
| Broker reports (Aon, Gallagher Re, Guy Carpenter) | publisher sites | PDF | event name | `metadata-only`: copyrighted commercial research |

A `metadata-only` source never yields a figure. When it is declared, the
adapter records a `publication_reference`: publisher, title, period, URL,
document digest and the decision with its reason. An `excluded` source cannot
be declared at all: the declaration is refused with `licence_excluded`. Both
appear in every coverage listing that the query layer and the MCP tools
return, with their reasons.

## Timestamps and the as-of cutoff

| Source | Reference period | Publication time | Cutoff clock |
| --- | --- | --- | --- |
| EIOPA statistics | reference date or year of the observation | the release date the operator declares per document (`release`), because the workbooks do not state one reliably **(verify)** | release date (end of day, UTC); without one, the first observation time |
| SFCR | reporting year (financial year end) | the publication date the operator declares per document or the report states **(verify)** | publication date (end of day, UTC); without one, the first observation time |
| NAIC market share (metadata-only) | data year | the publication date the operator declares | publication date |
| Catastrophe estimates | event dates as published | the publication or report date that each row states | the row's publication date (end of day, UTC) |

**Rule.** A revision is visible to a point-in-time query only when its
publication clock is at or before the query's public cutoff. If an acquisition
cutoff is also given, the revision must have been acquired by then. This is the
Market pack's `public_and_acquired` policy in `src/domains/market/asof.py`. A
publication date without a time counts as published at the end of that day
(UTC). A missing publication date is never replaced by the reference period.
The first observation time is used instead, which is conservative.

## EIOPA insurance statistics

* **Access:** public download of per-release statistics workbooks (XLSX) from the
  EIOPA insurance-statistics page. No authentication is needed. **(verify: download URL pattern per release,
  sheet names and whether a flat CSV export exists.)**
* **Content selected:** premiums written, claims incurred and expenses by line of business
  (solo undertakings, annual), and the balance-sheet, own-funds, SCR and MCR aggregates by
  country (quarterly "solvency and financial condition" statistics). The solvency ratio is
  stored only when EIOPA publishes it as a figure. Noesis never computes one.
* **Identifiers:** the country as published (ISO alpha-2, `EEA` and `EU` totals), the
  line of business as published (Solvency II LoB labels), the reference date and the
  dataset name. There is no per-undertaking identifier: EIOPA publishes aggregates only.
* **Markers:** confidential or not-reported cells carry published markers
  (for example `c`, `x`, `n/a`, `-`, `..`) **(verify the exact marker set)**. These are
  stored as the marker with a `null` value, never as zero.
* **Revisions:** every release is a vintage. A figure that changes in a later
  release adds a revision of the same observation. A figure that is unchanged
  adds nothing, but the release is recorded as seen.
* **Licence:** EIOPA legal notice: reproduction is authorised provided that the
  source is acknowledged (the EU institutions' reuse policy, Commission Decision
  2011/833/EU, as applied by EIOPA) **(verify)**. The attribution is
  "Source: EIOPA insurance statistics, <release>".
* **Rate limits:** none documented **(verify)**. Declared budget: one workbook per page, a
  60 s timeout, 20 MB, 12 pages per run, weekly at most.
* **Bounded coverage:** countries DE, FR, IT, NL and IE plus the EEA total; reference
  years 2022 to 2025; the premium, claims, SCR, own-funds and eligible-own-funds-to-SCR
  indicators as published. Other rows are counted in the page receipt as `out_of_scope`.
* **Decision:** `in-scope` (unverified-live).

## Insurers' SFCR reports

* **Access:** each insurer publishes its annual Solvency and Financial Condition Report
  as a PDF, usually on an investor-relations page. Supervisors do not host a central
  copy. **(verify each URL; URLs change every year.)**
* **Content quoted:** the public QRT annex. The adapter quotes declared cells only:
  * `S.02.01.02` (balance sheet): R0500 total liabilities, R1000 excess of assets over liabilities (C0010);
  * `S.05.01.02` (premiums, claims and expenses by line of business): R0110 gross premiums written, R0310 gross claims
    incurred (the declared LoB column);
  * `S.23.01.01` / `S.23.01.22` (own funds): R0800 eligible own funds to meet the SCR (C0010);
  * `S.25.01.21` / `S.25.02.21` / `S.25.03.21` (SCR): R0220 solvency capital requirement (C0100);
  * the solvency ratio "as reported" (`S.23.01.01` R0620, C0010) when the report states it.
  Each figure keeps its template code, row, column, unit and currency, and the quoted
  line. If a cell cannot be located or read, the figure is recorded as `unknown` with a
  reason. It is never estimated.
* **Identifiers:** the LEI stated in the report (`S.01.02` or the cover page), the
  reporting level (`solo` for an undertaking, `group` for a group SFCR) and the
  reporting year. A group report is never attributed to a subsidiary, or the other way
  round, without a recorded ownership link (GLEIF parent relationships, see IN07).
* **Corrections:** a republished or corrected SFCR (a new digest for the same insurer,
  year and report type) adds a revision. The earlier report is kept.
* **Licence:** the reports are published under a legal disclosure obligation; the
  insurers' website terms apply. Quoting the reported figures with attribution and a link
  is in scope. Mirroring the documents is not: Noesis keeps the digest, URL and the quoted
  cells, not the PDF.
* **Rate limits:** none documented. Declared budget: one report per page, 60 s, 30 MB,
  8 pages per run, monthly at most.
* **Bounded coverage (named sample):** group SFCRs for reporting years 2023 and 2024 of
  Allianz SE, Münchener Rückversicherungs-Gesellschaft AG (Munich Re) and AXA SA, and the
  solo SFCR of Allianz Versicherungs-AG for 2024. The LEIs are taken from the reports
  themselves **(verify against GLEIF)**; the declaration names the insurers and their
  reporting level only.
* **Decision:** `in-scope` (unverified-live).

## NAIC

* **Public material:** the annual property/casualty and life/A&H market share reports (PDF)
  and the company search (CIS) that shows NAIC company and group codes.
* **Terms:** content on `content.naic.org` is copyrighted by NAIC. Reproduction or
  redistribution needs NAIC's permission, and automated bulk retrieval from CIS is not
  offered **(verify)**.
* **Licensed products:** the Financial Data Repository, iSite+, the annual and quarterly
  statement data and InsData are subscription products. They are **never** acquired, and a
  declaration that names one is refused (`licence_excluded`).
* **Decision:** `metadata-only`. A declared NAIC market-share report is recorded as a
  `publication_reference` (title, data year, URL, digest). Its figures are not stored.
  The decision is encoded in the source declaration (`insurance.access_decision`)
  and restated in `LICENCE_DECISIONS`. The adapter refuses any other value that the
  audit has not granted.
* **If NAIC grants permission later:** a human decision recorded here can change the
  decision to `in-scope`. The adapter already reads a declared-column market-share CSV
  (`naic-market-share-csv`) keyed by NAIC company code and group code, with the data year
  and publication as published. That path is exercised offline only, with a test
  declaration.
* **Coverage effect:** no U.S. insurer statistics are answered. Every coverage listing
  says so.

## Catastrophe-loss publishers

Estimates from different publishers are **never merged**. Each publisher's
successive estimates for one event form that publisher's own series. Every new
publication is a revision of that series and never overwrites the one before.
The licence and attribution are stored on every estimate.

* **Florida OIR claims data:** the Office publishes catastrophe claims data reports
  (number of claims, claims closed and estimated insured losses as reported by
  insurers) for named hurricanes, updated in successive reports **(verify the CSV/XLSX
  availability and the column headers)**. These are Florida public records under
  Chapter 119, Florida Statutes. Reuse is permitted with attribution **(verify)**.
  Estimate type: `insured`. Decision: `in-scope`.
* **NOAA NCEI Billion-Dollar Disasters:** these are U.S. federal government data in the
  public domain. The CSV lists each event with its CPI-adjusted cost estimate
  (economic, not insured). Each yearly CPI adjustment is a new publication and so a
  revision **(verify)**. NCEI retired the product in 2025, so it is acquired as an
  archive. Estimate type: `economic`. Decision: `in-scope`.
* **PERILS, Verisk PCS:** licensed industry loss indices. Decision: `excluded`.
* **Swiss Re sigma, Munich Re NatCatSERVICE, Insurance Council of Australia, broker
  reports:** decision `metadata-only` until the reuse terms are confirmed (verify).

* **Event references:** an estimate keeps the event name and reference exactly as published.
  It is linked to a Natural Hazards event (`src/kb/hazards_*`) only through a published
  identifier (a GLIDE number, NHC storm ID or USGS event ID the row states) or an explicit
  citation. A name or date-proximity match is only a reviewable candidate (IN08).
* **Bounded coverage:** Florida OIR reports for 2022 to 2024 named hurricanes; NCEI events
  from 2020 to 2024 (archive). Budget: one document per page, 60 s, 10 MB, 6 pages per
  run, weekly at most.

## Licensing gaps that reduce scope

* No U.S. company-level statistics (NAIC is metadata-only).
* No global industry loss index (PERILS and PCS excluded). The only insured-loss
  series is Florida OIR's, which covers Florida hurricanes. Economic estimates come from
  NCEI, which is a U.S.-only archive.
* European catastrophe losses are therefore answered only through publishers cleared
  later. Until then, coverage reports them as `metadata-only`.

## Bounded coverage for live verification (IN13)

One EIOPA release for DE, and the Allianz SE group SFCR for 2024 (SCR, eligible own
funds and the solvency ratio as reported). Florida OIR reports for one 2024 hurricane,
with at least two successive publications, and the NCEI archive row for the same
event. The NAIC 2024 P&C market share report as a publication reference.
