# Competition cases and state aid: source-contract audit and bounded coverage (CS01)

Tracking: #2217 · delivery issue #2312 · recorded 2026-09-30.

This audit sets out, per source, what the Corporate Ownership pack's optional
`competition` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, fields and terms come from the publishers'
documentation and public pages as the author knows them. **Every item marked
_verify_ must be checked against the live pages, the live terms and a real
response before the first dated live run (CS14, #2368). No source is `live`
until that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `INSTRUMENTS`, `IDENTIFIERS`, `BOUNDED_COVERAGE`,
`DECLINED` and `LIVE_VERIFICATION` in `src/ingestion/competition_sources.py`;
each source entry in `config/source_packs/corporate-ownership.json`
(`corporate-ownership` 1.1.0, earlier sources verbatim) states
`competition.live_verification: unverified-live`, and the MCP tool
`competition_source_contracts` returns the same decisions.

Non-goals for every source: no prediction of case outcomes, no assessment of
market power or market definition, no assessment of the compatibility or
legality of aid, no legal advice. Stage names, decision types, case states and
award statuses are kept **as the authority published them**.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `ec-competition-cases` | European Commission, DG Competition - competition case search (`competition-cases.ec.europa.eu`) | merger (`M.`), antitrust and cartel (`AT.`) and state-aid (`SA.`) cases: title, instrument, sector, parties or member state as published, dated events, decision documents with OJ/CELEX references | bounded case-detail acquisition from the search application's JSON backend, one declared case number per unit (_verify_ the backend path `/api/cases/{case_number}` and field names; the fallback is the documented case-detail page, same fields) | `unverified-live` |
| `eu-state-aid-tam` | European Commission - State Aid Transparency Award Module (TAM) public search | individual awards above the transparency thresholds: beneficiary name, national identifier and type, granting authority, aid instrument, objective, SA measure reference, nominal amount or amount range and currency, date of granting | bounded public-search export by member state and SA measure (`/competition/transparency/public/api/awards?countryCode=..&saNumber=..`, _verify_ the export path and field names); one member state and measure per unit; a result longer than one page is `budget_exhausted`, never truncated | `unverified-live` |
| `uk-cma-cases` | UK Competition and Markets Authority via GOV.UK | CMA case pages (`cma_case` specialist documents): case type, case state, market sector, opened/closed dates, dated change history, attached decisions and reports | GOV.UK Content API `https://www.gov.uk/api/content/cma-cases/{slug}` (documented, no key); one declared slug per unit | `unverified-live` |
| `us-ftc-cases` | US Federal Trade Commission - legal library, cases and proceedings | FTC case pages: matter number, docket number, case status, type of action, respondents as named, dated case timeline with the published documents (complaint, proposed consent order, decision and order, closing statement) | bounded page acquisition of the declared case page `https://www.ftc.gov/legal-library/browse/cases-proceedings/{slug}`; the Drupal field classes read (`field--name-field-matter-number`, `...-respondents`, `case-timeline__item`) are _verify_ | `unverified-live` |
| `us-doj-atr-cases` | US Department of Justice, Antitrust Division - case filings | Antitrust Division case pages: case type as published (civil merger, civil non-merger), open date, defendants as named, court docket number, dated case documents (complaint, proposed final judgment, competitive impact statement, final judgment) | bounded page acquisition of the declared case page `https://www.justice.gov/atr/case/{slug}`; field classes are _verify_ | `unverified-live` |

## Per-source contract

| Source | Reuse terms and attribution | Rate limits and pagination | Revision model | Documents |
| --- | --- | --- | --- | --- |
| EC case search | Commission reuse policy, Commission Decision 2011/833/EU: reuse with acknowledgement of the source ("Source: European Commission, DG Competition") | no published limit (_verify_); one request per declared case, at most 20 cases per source per run | a changed case payload (new event, document or state) is a new revision of the case record; stage records are append-only (one record per published event, never deleted); earlier revisions stay queryable | decision documents are **linked, not mirrored**: type, date, language, URL, OJ reference and CELEX as published |
| TAM | Commission reuse policy (Decision 2011/833/EU); data are published by the granting member states under Article 9 GBER / the transparency communication | no published limit (_verify_); one request per member state and SA measure, page size 100; a longer result is `budget_exhausted` | an award is keyed by its TAM award identifier and member state; a correction (changed amount, date, beneficiary) or a withdrawal is a new revision of the award record; earlier revisions stay queryable | none (tabular) |
| GOV.UK CMA | Open Government Licence v3.0; attribution `OGL_ATTRIBUTION` | GOV.UK Content API: 10 requests/s per client is documented guidance (_verify_); one request per slug | `public_updated_at` is the page revision: a changed stamp or content is a new case revision; each `details.change_history` entry becomes an append-only stage record citing the page revision it was first seen in | attachments are linked (title, URL, content type, publication date) |
| FTC legal library | US government work, public domain (17 U.S.C. § 105); credit the FTC | no published limit for ftc.gov pages (_verify_); one request per declared page, at most 20 pages per run | the page's `article:modified_time` meta (or the page hash when absent) is the revision stamp; timeline items are append-only stage records | documents are linked (title, date, URL) |
| DOJ Antitrust Division | US government work, public domain; credit the Antitrust Division | as for FTC (_verify_) | as for FTC | documents are linked; court docket numbers are stored **as citations only** - court dockets are acquired by the Legal pack's `courts` feature, never here |

**Unavailable-access fallback.** A failed unit (HTTP error, redirect to another
host, schema drift, a result longer than one page) fails the run for that
source with its code; earlier revisions stay current and nothing is marked
closed, decided, withdrawn or revised because of a failure.

## Instruments in scope, and what is excluded

| Source | In scope (instrument as recorded) | Excluded, and why |
| --- | --- | --- |
| EC | merger control (`M.` - EU Merger Regulation), antitrust and cartels (`AT.` - Articles 101/102 TFEU), state aid (`SA.` - Articles 107-109 TFEU) | Foreign Subsidies Regulation (`FS.`) and Digital Markets Act (`DMA.`) cases: separate regimes outside the tracker's scope; national competition authority cases (ECN) |
| TAM | individual aid awards published in TAM | aid below the transparency thresholds (not published), de minimis registers (national, not in TAM), aggregate scheme budgets (not awards) |
| CMA | mergers, markets (market studies and market investigations), Competition Act 1998 and cartels cases | consumer-enforcement and regulatory-appeal cases (`consumer-enforcement`, `regulatory-references-and-appeals`): not competition cases in the tracker's sense; Subsidy Advice Unit reports: advisory, not awards |
| FTC | competition matters: mergers and antitrust (Section 5 FTC Act unfair methods of competition, Section 7 Clayton Act) | consumer-protection matters (the bulk of the legal library): outside scope; HSR **early-termination notices** (`api.ftc.gov` HSR early-termination list): declined - the grants were suspended in 2021 and a notice is not a case action, so the list is documented here and not acquired |
| DOJ | civil merger and civil non-merger (Sherman Act, Clayton Act) actions | **criminal** antitrust prosecutions: declined - defendants are frequently natural persons and a criminal case page is not needed for company-to-case answers; court dockets (Legal pack `courts` feature) |

## Stable native identifiers (stored as published)

| Source | Identifiers |
| --- | --- |
| EC | case number `M.nnnnn` / `AT.nnnnn` / `SA.nnnnn` (prefix and number as published); decision numbers `C(yyyy) nnnn`; CELEX (`3yyyyMnnnnn`, `3yyyyDnnnn`); OJ references (`OJ C 123, 1.10.2025, p. 5`) |
| TAM | TAM award identifier and member state (`countryCode`); SA measure number `SA.nnnnn`; beneficiary national identifier with its published type (e.g. KvK number, VAT number, company registration number) |
| CMA | GOV.UK slug (`/cma-cases/{slug}`) and content id; case reference when published in the body (e.g. `ME/1234/25`) |
| FTC | FTC matter number (`2510001`), administrative docket number (`C-4999`, `D-9999`) or federal court civil action number (citation only) |
| DOJ | case page slug, court civil action number (citation only, e.g. `1:25-cv-09999`) |

## Party and beneficiary data

Parties are companies, public bodies and member states. Party records keep the
name and role exactly as published (notifying party, target, addressee,
beneficiary, complainant, respondent, defendant). No identity is resolved when
a record is stored; matching to ownership entities happens only through
reviewable candidates (CS07). CMA pages publish no structured party list: the
parties are the undertakings the case title names (`Acquirer / Target merger
inquiry`), recorded with the role "named in case title" (_verify_ the naming
convention per case). Natural persons named in a case (e.g. an individual
respondent) are not recorded as parties.

## Bounded coverage

* **Seed companies and groups** come from the ownership layer: the groups
  already acquired by the Corporate Ownership sources (the fictional Exampla
  and Northwind groups in the fixtures). Cases are selected by declared case
  number, slug or measure - never by crawling a search result.
* **Date window**: cases opened or awards granted from 2020-01-01 onwards;
  earlier stages of an in-window case are kept.
* **Per run**: at most 20 declared units per source; TAM results of at most
  100 awards per unit.

## LIVE_VERIFICATION

| Provider | Intended status | Current status |
| --- | --- | --- |
| `ec-competition` | `verified-live` after a dated run of the declared cases | `unverified-live` |
| `eu-tam` | `verified-live` after a dated run of the declared measures | `unverified-live` |
| `uk-cma` | `verified-live` after a dated run of the declared slugs | `unverified-live` |
| `us-ftc` | `verified-live` after a dated run of the declared pages | `unverified-live` |
| `us-doj` | `verified-live` after a dated run of the declared pages | `unverified-live` |

A monitor withholds revisions acquired *live* from an `unverified-live`
provider until the dated run verifies it; fixture replays are notified and
marked as fixture evidence.
