# Campaign finance: source-contract audit, donor data minimisation and bounded coverage (CF01)

Tracking: #2209 · delivery issue #2473 · recorded 2026-09-30.

This audit sets out, per source, what the Political pack's campaign-finance
features (`campaign-finance-us`, `campaign-finance-uk`) may acquire, how, on
what terms, and how the personal data of individual donors is minimised. It was
written without network access. Endpoints, fields and terms come from the
providers' published documentation as the author knows it. **Every item marked
_verify_ must be checked against the live documentation, the live terms and a
real response before the first dated live run (CF14, #2529). No source is
`verified-live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `MINIMISATION` in
`src/ingestion/campaign_finance_sources.py`. Each source entry in
`config/source_packs/political.json` (`official-political-records` 1.4.0)
states `campaign_finance.live_verification: unverified-live`, and the MCP tool
`campaign_finance_source_contracts` returns the same decisions.

Non-goals for every source: no influence scoring, no "dark money" inference,
no inference about undisclosed funding sources, no quid-pro-quo reading of
co-occurring records, and no profiling of private individual donors. Figures
are kept as each regulator published them; amended filings never overwrite
the versions they amend and are never re-aggregated without naming the
versions used.

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `us-fec-committees` | OpenFEC API v1 (`/committee/{id}/history/`) | committee registration (Form 1) state per two-year period: type, designation, party, affiliated committee, candidate ids | `unverified-live` | Documented API with an api.data.gov key; fixture-verified parser; field names are _verify_ |
| `us-fec-candidates` | OpenFEC API v1 (`/candidate/{id}/history/`) | candidate registration per two-year period: name, office, state, district, party, election years | `unverified-live` | As above; the candidate is a registered public filer and is stored as published |
| `us-fec-filings` | OpenFEC API v1 (`/committee/{id}/filings/`) | every filing version of a committee in a cycle: file number, form and report type, coverage period, receipt date, amendment indicator and chain, most-recent flag, summary totals | `unverified-live` | `amendment_chain`, `most_recent`, `most_recent_file_number`, `previous_file_number` are _verify_ |
| `us-fec-schedule-a` | OpenFEC API v1 (`/schedules/schedule_a/`) | itemised receipts of the declared committees per two-year period | `unverified-live` | Keyset pagination (`last_index`, `last_contribution_receipt_date`) is _verify_; individual contributors are minimised (below) |
| `us-fec-schedule-b` | OpenFEC API v1 (`/schedules/schedule_b/`) | itemised disbursements of the declared committees per two-year period | `unverified-live` | As above (`last_disbursement_date`); individual payees are minimised |
| `us-fec-schedule-e` | OpenFEC API v1 (`/schedules/schedule_e/`) | independent expenditures (Schedule E of Form 3X/5 and 24/48-hour reports) of the declared spenders | `unverified-live` | `support_oppose_indicator`, `is_notice`, `dissemination_date`, `filing_form` are _verify_ |
| (not acquired) | FEC bulk data (`https://www.fec.gov/data/browse-data/?tab=bulk-data`) | whole-cycle files (committee master, candidate master, `itcont`, `oth`, `pas2`, independent expenditures) | `documented-not-acquired` | A whole-cycle file is not a bounded selection; bulk files are reserved for the live cross-check in CF14 |
| `uk-ec-donations` | Electoral Commission search, CSV export of donations | reportable donations and loans accepted by the declared regulated entities in a declared date window | `unverified-live` | The export URL, its query parameter names (including the regulated-entity filter) and columns are _verify_; individual donors are minimised |
| `uk-ec-spending` | Electoral Commission search, CSV export of campaign spending | spending return items of the declared regulated entities for a declared election | `unverified-live` | As above; supplier status is not published in the export (_verify_) |

## Per-source contract

### OpenFEC API (committees, candidates, filings, Schedules A, B and E)

* **Endpoints.** `https://api.open.fec.gov/v1/committee/{committee_id}/history/`,
  `/candidate/{candidate_id}/history/`, `/committee/{committee_id}/filings/`,
  `/schedules/schedule_a/`, `/schedules/schedule_b/`, `/schedules/schedule_e/`.
* **API-key handling.** An api.data.gov key (required secret
  `NOESIS_OPENFEC_API_KEY`) is sent as the `X-Api-Key` header, never as the
  `api_key` query parameter, so it never appears in a URL, receipt, record or
  log. `DEMO_KEY` is never used.
* **Licence and reuse restrictions.** FEC data is a US government work in the
  public domain, **but** 52 U.S.C. 30111(a)(4) and 11 CFR 104.15 forbid using
  information copied from reports about individual contributors "for the
  purpose of soliciting contributions or for commercial purposes"; the FEC
  seeds reports with fictitious "salted" names to detect misuse. Noesis is a
  non-commercial research tool, never solicits, and additionally does not store
  individual contributors' names or addresses at all (see the minimisation
  decision), so no stored item can be used for solicitation. Committee names
  and addresses may be used to solicit committees under the statute; Noesis
  still does not export contact lists. _verify_ the current wording.
* **Rate limits.** 1,000 requests per hour per key (7,200 on request);
  api.data.gov answers HTTP 429 with `Retry-After` (_verify_). One bounded
  selection per run.
* **Pagination.** Offset pages (`page`, `per_page` ≤ 100) for history and
  filings; keyset pages (`last_index` plus the sort column's last value) for
  Schedules A, B and E. A unit is all-or-nothing: at most five pages of 100 per
  unit; a longer list is `budget_exhausted`, never truncated.
* **Identifiers.** Committee ID (`C` + 8 digits), candidate ID (office letter
  + 8 characters), filing `file_number`, `image_number`, line-item `sub_id`
  and `transaction_id`.
* **Amendment model.** Each filing version has its own `file_number`.
  `amendment_indicator` is `N` (new), `A` (amendment) or `T` (termination);
  `amendment_chain` lists the file numbers from the original to this version;
  `most_recent` and `most_recent_file_number` state which version the FEC
  currently treats as the latest. Noesis stores every version as its own
  record, stores `most_recent` as published at each observation (a change of
  the flag is a new revision of that version's record, never an inference) and
  never re-aggregates across versions. Line items are keyed by the file number
  they were reported in, so amended line items stay distinct. Memo items
  (`memo_code = X`) are kept and labelled; they are never counted.
* **24/48-hour reports.** Schedule E rows from 24/48-hour notices and from the
  later periodic report that covers the same expenditure carry different file
  numbers and are kept as separate source assertions; nothing is
  deduplicated.

### FEC bulk data

Documented for completeness: whole-cycle ZIP files of fixed-width or
pipe-delimited records with header files. Not acquired in the first coverage
because a cycle file cannot be bounded to the declared committees without
downloading it in full; the live validation (CF14) may use it to cross-check
API totals.

### UK Electoral Commission search (donations and spending)

* **Endpoints.** `https://search.electoralcommission.org.uk/api/csv/Donations`
  and `.../api/csv/Spending` (_verify_ both paths and every parameter name:
  `regulatedEntityId`, `from`, `to`, `rows`, `start`, `sort`, `order`). If the
  service offers no regulated-entity filter, the adapter keeps only the rows
  whose `RegulatedEntityId` is declared and reports the rest as out of
  selection in the receipt.
* **Authentication.** None.
* **Licence.** Open Government Licence v3.0 for Commission content, with the
  attribution "Contains Electoral Commission data" (_verify_ that the search
  data is covered). The registers are published under the Political Parties,
  Elections and Referendums Act 2000 (PPERA); individual donors are natural
  persons and their data is personal data under the UK GDPR, which is the
  reason for the minimisation decision below.
* **Rate limits.** Undocumented (_verify_); one request per declared unit.
* **Identifiers.** `ECRef` per donation or spending item, `RegulatedEntityId`,
  `DonorId`, `CompanyRegistrationNumber` for company donors.
* **Revision model.** The Commission corrects a published item in place: the
  same `ECRef` with different values in a later export is a *revision* of that
  item, linked to its predecessor. A donation reported late appears in a later
  export with an `AcceptedDate` in an earlier period; it is stored as a new
  item of its return and flagged as observed after that return was first
  acquired. Nothing is dropped when an item disappears from a later export.
* **Totals.** The search export publishes items, not return totals. Answers
  say that no total is reported; any sum is labelled derived and lists the
  item revisions it used.

## Donor data-minimisation decision (binding for every campaign-finance module)

Individual donors are natural persons. Regulators publish their names because
the law requires disclosure of donations above a threshold, not so that
private persons can be profiled. The decision follows the rule "never store or
expose more personal data than the regulator's publication allows, and less
where the research purpose does not need it":

1. **Who is an individual.** FEC entity types `IND` (individual) and `CAN`
   (a candidate contributing personally), and Electoral Commission donor
   status `Individual`, are natural persons. The same applies to natural-person
   *payees* (FEC Schedule B and E payees with entity type `IND`). Committees,
   PACs, parties, organisations, companies, trade unions, LLPs, friendly
   societies, trusts and unincorporated associations are organisations.
   Supplier names in the Commission's spending export (whose status is not
   published) are kept as published and treated as organisations; this is a
   known limitation recorded for CF14.
2. **Stored for an individual line item.** Amount and date as reported, the
   filing version and schedule it was reported on, memo flag, receipt or
   disbursement type, the regulator's own line reference (FEC `sub_id` /
   `transaction_id` / image number, Commission `ECRef`), donor status or
   entity type as published, and for the Commission the published
   `IsAggregation` flag.
3. **Never stored (redacted at acquisition, before any document, record or
   receipt is written).** Name, street address, city, ZIP or postcode, employer,
   occupation, the regulator's donor or contributor id, and any per-person
   year-to-date aggregate. The parser drops these fields and lists them under
   `minimisation.withheld`; the record store refuses (`minimisation_violation`)
   any individual item that still carries one of them, so a faulty adapter
   cannot write them. The raw response bytes are never retained; receipts hold
   only their digests.
4. **Aggregated as published.** Filing summary totals (for example the FEC's
   itemised and unitemised individual contributions) are stored exactly as the
   regulator reported them. Noesis never aggregates individual items per
   person, and never groups items by a person because it holds no identifier
   that could.
5. **Never matched.** Individual donors and payees are excluded from entity
   matching, identity proposals, affiliate expansion, lobbying and ownership
   links; they stay visible only as minimised, unmatched line items.
6. **Who may query them.** Minimised individual line items are returned only
   to principals holding `knowledge:political:campaign-finance:individual-items:read`
   in addition to the read scope. Everyone else sees, per filing version, the
   count of withheld individual items and the regulator's reference to the
   published filing. Organisational donors and payees are returned with the
   read scope.
7. **Retention.** Minimised individual items are retained with the filing
   version they belong to and carry no personal identifier; there is nothing
   personal to purge. Should a regulator withdraw or redact an item, the next
   acquisition records the change as a revision; earlier revisions keep only
   the minimised fields. No automatic expiry is implemented in the first
   coverage.
8. **Notices and exports.** Monitor notices and evidence bundles follow the
   same rules: they count individual items and cite filings, and they never
   name an individual donor.

## Bounded first coverage

* **US (FEC):** the committees, candidates and committee cycles named in each
  source's `campaign_finance.selection` (at most 50 units per source, five
  pages of 100 rows per unit); the first coverage is one presidential
  candidate, their principal campaign committee, one PAC and one party
  committee making independent expenditures, for one two-year period. Filings
  of all versions (original, amendments, terminations) within the declared
  cycles are acquired so amendment chains are complete.
* **UK (Electoral Commission):** the regulated entities named in each
  selection (at most 50), donations within a declared accepted-date window of
  at most one year, and spending returns for one declared election; at most
  2,000 rows per unit (an export at the cap is `budget_exhausted`).
* **Contests:** the contests reached through accepted identity matches between
  FEC candidates or Commission regulated entities and the elections feature's
  records (#1908).
* Nothing implies complete coverage of a cycle, a party or an election.

## LIVE_VERIFICATION

Every source is `unverified-live`. Offline evidence is the authored fixtures
under `tests/fixtures/campaign_finance/` and
`tests/fixtures/source_packs/political-campaign-finance-*.json` (fictional
committees, candidates and organisations; individual donors appear only in the
native fixture responses with placeholder names that the parser discards).
