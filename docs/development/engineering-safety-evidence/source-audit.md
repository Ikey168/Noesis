# Engineering Safety: source-contract audit and bounded coverage (ES01, #2062)

Tracking issue: #2059. This audit records, for each candidate source, the
endpoint, authentication, licence and terms, rate limits, stable identifiers,
how revisions and supersessions are expressed, and the access decision the
Engineering Safety pack implements (`implement`, `link-only` or
`not implemented`), with the bounded v1 coverage.

**Verification status.** No live request was made while writing this audit;
the build environment has no network access to FAA, EASA, NTSB, PHMSA, CSB,
NHTSA, BFU or BEA. Every claim marked **(verify)** comes from the providers'
public documentation as known at the time of writing and must be checked
against the live terms and services before live acquisition is accepted
(ES17, #2078, not part of this delivery). The adapters in
`src/ingestion/engineering_safety_sources.py` parse the documented shapes; the
offline fixtures under `tests/fixtures/source_packs/engineering-safety-*.json`
and `tests/fixtures/engineering_safety/variants.json` are **authored** and name
fictional manufacturers, operators, AD numbers, dockets, N-numbers and report
numbers. None of them is captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in
`src/ingestion/engineering_safety_sources.py` restate these decisions in code.
Every implemented source is `unverified-live` until a dated live run is
recorded under this directory. The source pack is
`config/source_packs/engineering-safety.json`; each source's
`engineering_safety.audit` field points at its section here.

## Summary

| Source | Endpoint | Format parsed | Decision |
| --- | --- | --- | --- |
| FAA ADs via the Federal Register API | `https://www.federalregister.gov/api/v1/` | document JSON + raw text | `implement` |
| FAA Dynamic Regulatory System (DRS) | `https://drs.faa.gov/` | n/a | `not implemented` |
| EASA AD publishing tool | `https://ad.easa.europa.eu/` | AD page, label/value rows | `implement` (terms for automated retrieval **(verify)**) |
| NTSB CAROL | `https://data.ntsb.gov/carol-main-public/` | case and recommendation JSON **(verify)** | `implement` |
| NTSB monthly datasets | `https://www.ntsb.gov/Pages/monthly.aspx` | Access databases | `not implemented` |
| PHMSA incident files | `https://www.phmsa.dot.gov/` | tab-delimited text | `implement` |
| U.S. Chemical Safety Board | `https://www.csb.gov/` | investigation HTML page | `implement` |
| NHTSA ODI investigations | `https://static.nhtsa.gov/` | tab-delimited flat file | `implement` |
| NHTSA complaints | `https://api.nhtsa.gov/` | JSON | `implement` (count-bounded) |
| NHTSA recalls | n/a | n/a | `not implemented` here: owned by the Products safety feature (#1916) |
| BFU (Germany) | `https://www.bfu-web.de/` | report page HTML (German) | `implement` (terms **(verify)**) |
| BEA (France) | `https://bea.aero/` | none | `link-only` |

## Bounded v1 coverage

The runtime's budgets apply per source: at most 50 pages per run, 200
records per page, 5 MB per response, 30 s per request.

| Source | v1 bound |
| --- | --- |
| FAA ADs | a declared list of at most 50 Federal Register document numbers for the declared aircraft types (the fixture set is the fictional Examplar EX-100 family); one document plus its raw text per page |
| EASA ADs | a declared list of at most 50 AD numbers, each revision suffix a separate selector |
| NTSB | at most 50 declared NTSB numbers and recommendation numbers |
| PHMSA | the declared hazardous-liquid file (at most 10 files), rows with `IYEAR` in the declared window (at most 10 years; v1 2024-2026) |
| CSB | at most 50 declared investigation pages |
| NHTSA ODI | the declared flat file, rows for the declared makes and models opened on or after the declared date |
| NHTSA complaints | at most 50 declared make/model/year selectors, each at most 200 complaints; a larger count is refused, never truncated |
| BFU | at most 50 declared report pages |
| BEA | at most 50 declared link-only entries |

## Overlap with #1916 (Products safety notices)

NHTSA recall campaigns, CPSC recalls, EU Safety Gate and RASFF stay with the
Products `safety` feature (`src/kb/product_safety.py`, source pack
`products-displays`). This pack never acquires a recall notice. An ODI
investigation's campaign numbers (`CAMPNO`, or a campaign number in its
summary) are stored as `cites_recall` relations and linked by exact campaign
number to the Products feature's `nhtsa` notices when a Products namespace is
named; otherwise they stay unresolved identifiers. Complaints are not
acquired by #1916, so there is no overlap there.

## FAA airworthiness directives

- **Endpoint.** `GET /api/v1/documents/{document_number}.json`, then the
  document's `raw_text_url` on the same host **(verify the raw-text path)**.
  FAA DRS is not used.
- **Auth, terms, limits.** No key. Federal Register content is a US federal
  publication in the public domain **(verify the legal-status page)**. Rate
  limits are not documented **(verify)**; one selector per page.
- **Identifiers.** AD number (`YYYY-NN-NN`), Federal Register document number,
  amendment (`39-NNNNN`), docket (`FAA-YYYY-NNNN`), project identifier. A
  record is keyed by AD number; the FR document number is kept per revision.
- **Revisions and supersession.** A superseding AD has a new AD number and
  states "This AD replaces AD …" in paragraph (b) Affected ADs; "revises"
  appears for revisions. A correction republishes the AD under a new FR
  document (`…; Correction` in the title) and becomes a new revision of the
  same AD. Supersession is read only from what the AD states.
- **Parsing.** Only the text after `§ 39.13 [Amended]` and up to `Issued on`
  is read. Lettered paragraphs are headings written as plain lines and are
  accepted only in sequence (a), (b), …, so a roman sub-item never opens a
  paragraph. Applicability is parsed into models, serial ranges and part
  numbers only when unambiguous (one model list and one serial statement);
  enumerated sub-items fall back to the verbatim clause. Effective date,
  compliance time and required actions are verbatim with the paragraph
  label and character span.
- **Not concluded.** No statement that an aircraft complies or is affected.

## EASA airworthiness directives

- **Endpoint.** `GET /ad/{ad_number}` on the AD publishing tool, parsed as
  label/value rows (`AD Number`, `Issue date`, `Effective date`,
  `Supersedure`, `Approval Holder / Type Designation`, `Subject`, `Reason`,
  `Required Action(s) and Compliance Time(s)`, `Related AD(s)`) **(verify
  page structure and labels)**. The AD PDF is linked, never mirrored.
- **Terms.** The EASA legal notice allows reuse with attribution; whether
  automated retrieval of AD pages is permitted is **not confirmed (verify)**.
  If the terms do not allow it, the decision falls back to `link-only` and the
  selection is emptied; no scraping beyond the declared AD numbers.
- **Identifiers and revisions.** A record is keyed by the AD number without
  its revision suffix; `2026-0123R1` is appended as a revision (`R1`) of
  `2026-0123`, never overwriting it. Dates are day-first (`dd/mm/yyyy`)
  **(verify)**. Cross-references to FAA or other ADs are stored only from the
  `Related AD(s)` row; no equivalence is inferred.

## NTSB CAROL

- **Endpoint.** CAROL JSON case view per NTSB number and recommendation view
  per recommendation number (`cases/{ntsb_number}`,
  `recommendations/{number}` under `/carol-main-public/api/`) **(verify both
  paths and field names)**. The monthly datasets are Access databases and are
  `not implemented`; CAROL serves the same bounded cases.
- **Auth, terms, limits.** No key; US federal public domain **(verify)**;
  rate limits not documented **(verify)**.
- **Identifiers.** NTSB number (`ERA26FA101` shape), recommendation number
  (`A-26-015` shape), N-number and serial as published.
- **Revisions.** A preliminary report and a final report are revisions of the
  same investigation; the final one ranks above the preliminary one and
  becomes current without deleting it. Probable cause and findings are
  verbatim with a JSON-pointer locator and the report status. A
  recommendation's `StatusHistory` gives every published status change with
  its date; each becomes a dated response record.
- **Location.** Coordinates are stored as published; nothing is geocoded
  from a place name.

## PHMSA incident files

- **Endpoint.** The published incident files (zipped on the PHMSA page; the
  declared URL is the unzipped tab-delimited text) **(verify file URL, column
  names and whether a vintage date is stated)**. The file vintage is declared
  per document.
- **Identifiers.** `REPORT_NUMBER`; operator as `OPERATOR_ID` and `NAME` as
  published. Operator resolution to canonical entities is a reviewable
  candidate only.
- **Revisions.** A report whose fields change in a later file vintage gets a
  new revision dated by that vintage; an unchanged row in a newer vintage
  adds nothing (row order and vintage are not part of the content).
- **Units.** Release quantities keep the native value and unit
  (`UNINTENTIONAL_RELEASE_BBLS` in barrels); a conversion through
  `src/integrations/units.py` is attached at read time when the unit is
  explicit and the unit library is available, never replacing the value.
- **Not concluded.** No operator safety score or ranking.

## US Chemical Safety Board

- **Access.** The CSB publishes pages and PDFs, not an API. One declared
  investigation page per request; details rows, `Key Findings`, `Causal
  Factors` (also when written as a bold paragraph) and the recommendations
  table (number, recipient, status, status date, text) **(verify structure
  and robots.txt)**. Videos, dockets and PDFs are never mirrored.
- **Identifiers.** Investigation page slug; recommendation number
  (`2026-02-I-TX-R1` shape).
- **Revisions.** The page shows only the current status of a recommendation;
  each acquisition that shows a new status adds a dated response, so the
  history grows across acquisitions.
- **Entities.** Company and facility names are source strings with
  reviewable candidates only.

## NHTSA ODI investigations and complaints

- **Investigations.** The ODI investigations flat file (tab-delimited, no
  header; declared column order) **(verify URL, column order, date format
  `YYYYMMDD` and action-number format)**, filtered to declared makes and
  models and an opening-date bound. Rows are grouped per action number (one
  row per make/model/year). An upgrade (PE → EA) is a relation read from the
  summary ("upgraded to …", "opened from …"); a closure is a revision dated
  by the closing date. Recall campaigns are links, never acquired.
- **Complaints.** `complaints/complaintsByVehicle?make=&model=&modelYear=`
  **(verify envelope and date format)**, at most the declared count per
  vehicle; a larger count is refused. Complaints are unverified consumer
  reports, stored as source records and never treated as confirmed defects.
  Partial VINs are never stored.

## BFU

- **Access.** Declared report pages on `bfu-web.de` (German) **(verify page
  structure, labels `Aktenzeichen`, `Berichtsart`, `Veröffentlicht`, and the
  terms of reuse)**. Findings (`Schlussfolgerungen`) and causes (`Ursachen`)
  are stored verbatim in German with the language recorded; any translation
  would be derived (through `src/kb/cross_language.py`) and never replaces
  the original. Safety recommendations come from the report's table.
- **Decision.** `implement`, bounded; falls back to `link-only` if the terms
  do not allow automated retrieval.

## BEA

- **Access.** Reports are PDFs without a documented machine interface; the
  terms of reuse are not confirmed **(verify)**.
- **Decision.** `link-only`: the source declares report entries (report id,
  title, language, publication date, link, and the aircraft as the title
  names it). Nothing is fetched or quoted. No equivalence with another
  authority's occurrence is inferred.
