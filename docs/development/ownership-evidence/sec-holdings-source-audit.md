# Source audit: SEC 13F, 13D and 13G filings as structured ownership assertions (#2883)

This extends the Corporate Ownership source audit (O01, the provider table in
`docs/subsystems/corporate-ownership.md`) for the SEC filings whose holdings
and percentages were previously kept as filing **references** only. The
machine-readable copy is `SEC_HOLDINGS_CONTRACT` and `LIVE_VERIFICATION` in
`src/ingestion/ownership_sec_holdings.py`; the SEC EDGAR provider contract in
`src/ingestion/ownership_providers.py` links to it (`structured_filings`).

Everything marked **verify** is an assumption made from the SEC's published
specification as summarised by secondary sources. It has not been checked
against a live filing from this runtime: `www.sec.gov` did not resolve from the
session's network. The connector therefore stays `unverified-live`, and its
fixtures are authored, not captured.

## Decision

| Item | Decision |
| --- | --- |
| Status | implemented, `unverified-live` |
| Connector | `sec-edgar-holdings` (new, `src/ingestion/ownership_sec_holdings.py`), source `sec-edgar-holdings` in `config/source_packs/corporate-ownership.json` 1.2.0 |
| Host | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-without-dashes}/{document}` only |
| Selection | 1–25 explicitly selected filings per source: CIK, accession, primary document, form, the filing date as listed by EDGAR, and for 13F the information-table document name |
| Forms parsed | `13F-HR`, `13F-HR/A`, `SCHEDULE 13D`, `SCHEDULE 13D/A`, `SCHEDULE 13G`, `SCHEDULE 13G/A` (XML) |
| Not parsed | Pre-2024-12-18 HTML/text schedules (`SC 13D`, `SC 13G`), Forms 3/4/5, `13F-NT` notices. They stay filing references from `sec-edgar-ownership`. A selected schedule document that is not the XML cover page becomes an unparsed filing reference (`parsed: false`). |
| Authentication | none; the fair-access policy requires a declared User-Agent with contact details. The connector reuses the pack's `NOESIS_SEC_CONTACT` secret and the EDGAR adapter's User-Agent (`Noesis corporate-ownership research <contact>`). A contact without `@` is refused before any request. |
| Rate limits | fair access: at most 10 requests per second (SEC, `https://www.sec.gov/os/accessing-edgar-data`). Live requests are spaced by the inherited EDGAR interval (0.12 s, at most about 8 per second), HTTP 429 `Retry-After` is honoured, and each document is fetched once per run. Information-table pages are cut from one cached response. |
| Cadence | 13F quarterly (due within 45 days of quarter end); schedules per triggering event and amendment. Re-runs are idempotent: unchanged content mints no revision. |
| Terms | SEC public data under the fair-access policy |
| Retained evidence | each primary document and information table as the runtime document with SHA-256; every record cites its accession number, document URL and an XPath-style locator |
| Cost | free |

## Formats as read (verify)

The authoritative definitions are the EDGAR technical specifications
(`https://www.sec.gov/submit-filings/technical-specifications`). A web search on
2026-11-02 reported that this page lists a "Schedule 13D & 13G" XML specification
at version 2.3, dated 2026-04-15 (**verify**). Its XSD files were not opened. Element names below
were cross-checked against an open-source parser that reads these documents
(dgunning/edgartools: `edgar/beneficial_ownership/schedule13.py`,
`edgar/thirteenf/parsers/infotable_xml.py`, `edgar/thirteenf/parsers/primary_xml.py`).
That parser is a secondary source, not the specification. The adapter matches
elements by local name, so namespaces do not affect the lookup.

**Form 13F cover page** (`primary_doc.xml`, namespace
`http://www.sec.gov/edgar/thirteenffiler`, verify): `headerData/submissionType`,
`headerData/filerInfo/filer/credentials/cik` (the CIK confirmation code beside it
is never read), `headerData/filerInfo/periodOfReport` and
`formData/coverPage/reportCalendarOrQuarter` (MM-DD-YYYY, **verify**),
`coverPage/isAmendment`, `coverPage/amendmentNo`,
`coverPage/amendmentInfo/amendmentType` (`RESTATEMENT` or `NEW HOLDINGS`),
`coverPage/filingManager/name`, `coverPage/reportType`,
`summaryPage/tableEntryTotal`, `summaryPage/tableValueTotal`,
`summaryPage/otherIncludedManagersCount`, `signatureBlock/signatureDate`.
Whether a 13F/A states the amended accession number (`previousAccessionNumber`)
is **unverified**. When absent it is listed as unknown, never guessed.

**Form 13F information table** (namespace
`http://www.sec.gov/edgar/document/thirteenf/informationtable`, verify):
`informationTable/infoTable` rows with `nameOfIssuer`, `titleOfClass`, `cusip`,
`figi` (optional; **verify** that it is in the current schema), `value`,
`shrsOrPrnAmt/sshPrnamt`, `shrsOrPrnAmt/sshPrnamtType` (`SH` or `PRN`), `putCall`
(`Put` or `Call`), `investmentDiscretion` (`SOLE`, `DFND` or `OTR`), `otherManager`,
`votingAuthority/Sole`, `votingAuthority/Shared` and `votingAuthority/None`.

**Value units (verify).** The SEC amended Form 13F so that filings made on or
after 2023-01-03 report `value` rounded to the nearest dollar instead of in
thousands (release number to verify). The adapter derives
`value_unit` from the filing date in the selection (`USD` or
`USD-thousands`). Without a filing date, it uses `USD` only for report periods
from 2022-12-31 on and otherwise leaves the unit unknown. The figure itself is
stored as stated and never rescaled.

**Schedule 13D** (namespace `http://www.sec.gov/edgar/schedule13D`, verify):
`headerData/submissionType`, `coverPageHeader/securitiesClassTitle`,
`coverPageHeader/dateOfEvent`,
`coverPageHeader/issuerInfo/{issuerCIK, issuerCUSIP, issuerName}`, and
`reportingPersons/reportingPersonInfo` with `reportingPersonCIK` or
`reportingPersonNoCIK`, `reportingPersonName`, `memberOfGroup`,
`citizenshipOrOrganization`, `soleVotingPower`, `sharedVotingPower`,
`soleDispositivePower`, `sharedDispositivePower`, `aggregateAmountOwned`,
`percentOfClass` and `typeOfReportingPerson`.

**Schedule 13G** (namespace `http://www.sec.gov/edgar/schedule13g`, verify):
`coverPageHeader/eventDateRequiresFilingThisStatement`,
`coverPageHeader/issuerInfo/{issuerCik, issuerCusip, issuerName}`, and
`coverPageHeaderReportingPersonDetails` with `reportingPersonName`,
`memberGroup`,
`reportingPersonBeneficiallyOwnedNumberOfShares/{soleVotingPower, sharedVotingPower, soleDispositivePower, sharedDispositivePower}`,
`reportingPersonBeneficiallyOwnedAggregateNumberOfShares`, `classPercent` and
`typeOfReportingPerson`. Item 5's `classOwnership5PercentOrLess` is also read.
Whether the 13G cover page carries a reporting-person CIK is **unverified**.
Without one, the amendment chain is keyed by the reporting person's name as
stated, and the record says so (`chain_basis`).

**Amendments (verify).** The location of `amendmentNo` and of
`previousAccessionNumber` (the amended filing) in the schedule XML is
**unverified**. The adapter reads them wherever they occur. Date formats on the
schedules (MM/DD/YYYY assumed; ISO also accepted) are **unverified**. The
`memberOfGroup` / `memberGroup` values are assumed to be the cover-page row-2
boxes `a` (member of a group) and `b` (not); any other value is kept verbatim in
`native` and the group box stays unknown.

## Records and semantics

Records use the new contract version `noesis-ownership-record-v2`
(`contracts/schemas/jsonschema/noesis-ownership-record-v2.json`). It is
registered as `ownership-record` 2.0.0 beside the unchanged v1.

- **13F row**: a `reported_holding` assertion (holder: filing manager; subject:
  the issuer as named in that filing) with `position` (issuer name, class,
  CUSIP, FIGI, amount and SH/PRN, value and unit, put/call, investment
  discretion, other managers, voting authority sole/shared/none) and `filing`.
  It states no percentage and no control, so it never enters the
  parent/subsidiary slots of the ownership graph.
- **Schedule cover page**: one `shareholding` assertion per reporting person
  with percent of class as an exact decimal string, aggregate amount, `powers`
  (sole/shared voting and dispositive) and `group` (the box as stated and the
  other group members on the filing). Group members' figures overlap and are
  never summed.
- **Amendment chain**: every assertion carries `filing.amendment`
  (`is_amendment`, number, 13F type, amended accession, `chain_key`). An
  amendment is a new record on the chain; it never rewrites the original
  filing's records. The `holders`/`holdings` graph queries pick the statement
  in force as of a date:
  - **13F**: the latest report period on or before the date per filing
    manager. A restatement replaces earlier rows; a new-holdings amendment adds
    rows.
  - **Schedules**: the latest event date on or before the date per issuer and
    reporting person.

  Statements that are no longer in force are returned as `superseded`, each
  with the accession of the filing that superseded it.
- **Identity**: issuers, managers and reporting persons are recorded per filing
  with the identifiers that filing states. They resolve only through reviewable
  candidates (`OwnershipIdentityService.propose`):
  - the same CIK is an `exact-identifier` candidate;
  - the same CUSIP issuer number is a `cross-referenced-identifier` candidate;
  - an LEI meets these records through the SEC submissions entity and the
    existing GLEIF and market-instrument cross-references.

  Unreviewed candidates that would widen an answer are listed under
  `identity.pending`.

## Natural persons

A reporting person whose `typeOfReportingPerson` includes `IN` is a natural
person.

By default, the `person` record and that person's assertion are
**owner-scoped** (`person_policy: ownership-person-owner-scoped-v1`). They are
visible to the acquiring principal and to ownership reviewers, and redacted for
everyone else. A redacted line still cites its accession number. The person's
name is withheld from group-member lists and filer lists.

With `NOESIS_PERSON_RESEARCH` on (`docs/security/person-research-mode.md`), the
records are stored unscoped under `ownership-person-research-v1`. Writing such a
record with the mode off is refused with `minimisation_violation`.

## Fixtures and live state

`tests/fixtures/ownership/sec-edgar-holdings.json` is authored and fictional.
It covers:

- four 13F filings: an original, a restatement, a later quarter and a
  new-holdings amendment, including put, principal and other-manager rows;
- a Schedule 13D with four group members: one natural person and one without
  a CIK;
- a Schedule 13G and its amendment, which drops to 5 percent or less.

The CUSIP issuer numbers `99990A` and `99990B` carry computed check digits, but
no such numbers are issued. Offline acceptance is in
`tests/unit/domains/test_sec_holdings_acceptance.py`, and unit tests are in
`tests/unit/ownership/test_ownership_sec_holdings.py`.

`LIVE_VERIFICATION` is `unverified-live`, because no live filing was fetched. To
verify, rerun `scripts/ownership_live_check.py` with the `sec-edgar-holdings`
source added (not yet wired into the script), from a network that reaches
`www.sec.gov`, with `NOESIS_SEC_CONTACT` set. Then compare the element names,
date formats and amendment fields above against real filings and the current
XSDs.
