# Economics public finance: source-contract audit and provider coverage (B01)

Tracking: #1909 · delivery issue #1919 · recorded 2026-09-27.

This audit sets out, per source, what the Economics pack's optional
`public-finance` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, file layouts, identifiers and terms come
from the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live page and a
published file before the first dated live run (B12, #2014). No provider is
`live` until that run exists.** The machine-readable copy of these decisions
is `PROVIDER_CONTRACTS` in `src/ingestion/public_finance_sources.py`, and the
MCP tool `public_finance_source_contracts` returns it.

These non-goals apply to every source. Figures are what the issuing authority
published, each with its accounting basis and source revision. Plan,
supplementary-plan, outturn and payment records stay distinct. Nothing
forecasts a fiscal outcome or determines waste or fraud, and no figures on
different accounting bases are netted without a cited method.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| Bundeshaushalt open data (bundeshaushalt.de) | plan (Soll), supplementary plans, outturn (Ist) | `unverified-live` | The parser follows a semicolon CSV with Einzelplan/Kapitel/Titel codes and labels, an E/A marker and the amount columns the manifest declares. The `Stand`, unit and year header lines, the column labels and the per-year file URLs are _verify_ |
| Berlin Senate finance budget data (berlin.de / daten.berlin.de) | plan (Soll), outturn (Ist) | `unverified-live` | The parser follows a per-row table with Bereich/Einzelplan/Kapitel/Titel, `Jahr`, `BetragTyp` and `Betrag`. The column names, the file URL and the Bereich 31-42 → district crosswalk are _verify_ |
| EU Financial Transparency System | beneficiary commitments (and payments where a row says so) | `unverified-live` | The parser follows the yearly CSV export (year, beneficiary, VAT, country, budget line, programme, position key, amount). The column names and export URL are _verify_. The file states no publication date, so the HTTP `Last-Modified` header is used, and a file without one is refused. A full year may exceed the runtime's 100,000-row ceiling. Such a file is refused, never truncated, and needs a bounded selection (_verify_) |
| EU budget pages (commission.europa.eu) | budget overviews | `not-implemented` | No supported machine-readable figures (HTML and PDF), so nothing is scraped. The annual budget and amending budgets are legal acts acquired through CELLAR (`legal.works`), and payments come from FTS |
| Eurostat government finance statistics | ESA 2010 series | `unverified-live` | Acquired as JSON-stat through the existing Eurostat dataset connector with the dataset's `updated` time as the vintage. The dataset codes and filters (`gov_10a_main`, `gov_10a_exp`, `gov_10dd_edpt1`) are _verify_ |
| Bundesrechnungshof reports | audit findings | `not-implemented` (automated) | There is no documented machine-readable export, so reports are never scraped. Findings enter as operator-recorded finding sheets (report, passage locator, quoted text, cited lines) through `import_audit_findings`, with no verdict field |
| IMF GFS | GFSM 2014 series | `not-implemented` | **Reuse terms decision: blocked.** The IMF terms of use restrict redistribution of some datasets (_verify_ in writing). No acquisition is planned until the terms for storing and redistributing GFS series are recorded. Eurostat GFS covers EU members on ESA 2010 |

**Unavailable-access fallback.** When a file cannot be fetched (HTTP error,
redirect to another host, budget exceeded, schema drift, an undeclared amount
column or amount type, an unknown unit, or no stated publication date), the
run records the failure class and writes no release. Earlier releases stay
authoritative for their dates. A release is all-or-nothing, so a partial file
is never stored.

**Live evidence and notifications.** Releases acquired through the runtime's
HTTPS transport are `live` evidence, and fixture replays are `fixture`
evidence. Monitors withhold live releases of a provider whose decision is
still `unverified-live` until a dated live run moves it to `verified-live`.

## Per-source contract

| Source | Access | Auth / limits / pagination | File formats | Cadence | Retained evidence |
| --- | --- | --- | --- | --- | --- |
| Bundeshaushalt | one CSV per budget document per year (_verify_ URLs) | none; undocumented limits; no pagination, one file per page, at most 4 declared documents per run | CSV (semicolon, German number format) | budget law, each Nachtragshaushalt, preliminary Ist, Haushaltsrechnung | figures, file digest, Stand date, URL |
| Berlin | one CSV per Doppelhaushalt (_verify_) | none; one file | CSV (semicolon) | per budget cycle; Ist annually | figures, file digest, Stand or Last-Modified date, URL |
| EU FTS | one CSV per financial year (_verify_) | none; one file; ≤ 100,000 rows per run | CSV (XLSX not used) | annual, by mid-year following | rows, file digest, Last-Modified date, URL |
| Eurostat GFS | JSON-stat per declared series (dataset, geo and a filter for every multi-category dimension) | none; fair use (_verify_); ≤ 3 series per run | JSON-stat 2.0 | April/October EDP notifications | series vintages in the dataset store and economic release snapshots |
| Bundesrechnungshof | report pages and PDFs | not applicable (operator sheets) | finding sheet JSON | per report | report URL, passage locator, quote, cited lines |

## Hierarchy, fiscal-year, vintage, unit and accounting-basis semantics

| Source | Hierarchy identifiers (kept as published) | Fiscal year and vintages | Currency and units | Accounting basis |
| --- | --- | --- | --- | --- |
| Bundeshaushalt | Einzelplan (2 digits), Kapitel (4 digits), Titel (5 digits), optional Titelgruppe; E/A side | Haushaltsjahr. Soll per budget law, and a Nachtragshaushalt is its own plan of the same year. The Ist vintage is the file's Stand date, and the Haushaltsrechnung supersedes preliminary Ist (_verify_) | EUR, stated in thousand EUR (_verify_ the unit line) | cash (Kameralistik) |
| Berlin | Bereich (30 Hauptverwaltung; 31-42 districts, _verify_), Einzelplan, Kapitel, Titel; Titelart | Jahr column (a Doppelhaushalt states two years) | EUR (_verify_) | cash |
| EU FTS | budget line number and name (e.g. `01 02 01 01`), programme name, commitment position key | Year = financial year of the commitment. A republished year is a new vintage | EUR | commitment (legal commitments). A row typed as payment is recorded on a payment basis |
| EU budget (CELLAR acts) | heading, programme and budget line inside the act | annual budget plus amending budgets | EUR | commitment and payment appropriations |
| Eurostat GFS | dataset dimensions: geo, sector (S13...), na_item (TE, TR, B9...), cofog99, unit | calendar year. The vintage is the dataset `updated` time, and EDP notifications revise earlier years | unit dimension as published (MIO_EUR, PC_GDP) | ESA 2010 (accrual) |
| IMF GFS | GFSM classification | per release (not acquired) | national currency | GFSM 2014 (accrual) |

Figures on different bases (cash plan against ESA 2010 series, commitment
against payment) are shown side by side with a note and no difference. A
difference is computed only between figures on the same basis and currency,
unless a reconciliation method from the source is recorded with its citation.

## Terms

| Source | Terms as recorded | Retention |
| --- | --- | --- |
| Bundeshaushalt | Datenlizenz Deutschland - Namensnennung 2.0 (_verify_ on the open-data page) | figures and file digest |
| Berlin | CC BY 3.0 DE for daten.berlin.de datasets (_verify_ per dataset) | figures and file digest |
| EU FTS | Commission reuse policy, Decision 2011/833/EU, attribution (_verify_). Natural persons may appear only in aggregated form (_verify_ handling) | rows as published |
| EU budget pages | Commission reuse policy (_verify_) | none (not implemented) |
| Eurostat | Eurostat reuse policy, attribution required | series vintages |
| Bundesrechnungshof | reuse of official reports with attribution (_verify_ on the imprint) | the quoted passage and locator only |
| IMF | IMF terms of use (_verify_ in writing; decision **blocked**) | none |

## Pinned fixtures

Every source-pack entry replays authored files in the documented shape
through the real adapter. The files name fictional budget lines (Einzelplan
98), beneficiaries and figures, and the Eurostat cube states in its label
that its values are fictional. The fixture digest and expected output hash
are pinned in `config/source_packs/economic.json`
(`economic-statistics-and-filings` 1.2.0). Fixtures are never evidence of
live coverage. The Bundesrechnungshof finding sheet
(`tests/fixtures/public_finance/brh_bemerkungen_2100.json`) goes through the
operator import path, because there is no machine access to replay.
