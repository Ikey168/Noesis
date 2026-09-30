# Political campaign finance: contributions, expenditures and committee filings

The Political pack's optional `campaign-finance-us` and `campaign-finance-uk`
features (tracking issue #2209) add the filings campaign-finance regulators
publish - US Federal Election Commission committees, candidates, filing
versions, itemised receipts and disbursements and independent expenditures,
and UK Electoral Commission donation reports and campaign spending returns -
beside the existing lobbying, elections and legislation features. Every item
carries its source, record revision, the filing revision it was reported in
and the time it was observed. Amendments are kept as revisions, never
overwrites; donor-to-entity matches are reviewable identity assertions.

**Exclusions.** No influence scoring, no "dark money" or undisclosed-funding
inference, no quid-pro-quo reading of co-occurring links, and no profiling of
individual donors. Totals are the totals the regulator reported; a sum across
filings is only given on request, labelled `derived` and listing the filing
versions it used.

## Donor data minimisation (CF01)

Individual donors are natural persons. The binding decision in
`docs/development/campaign-finance-evidence/source-audit.md` governs every
module:

* FEC entity types `IND` and `CAN` and Commission donor status `Individual`
  keep only amount, date, filing version, schedule, memo flag, type and the
  regulator's line reference. Name, address, city, ZIP or postcode, employer,
  occupation, donor or contributor id, per-person aggregates and memo text are
  dropped by the parser before any record, document or receipt exists; the
  record store refuses (`minimisation_violation`) anything that still carries
  them.
* Individuals are never matched, linked or expanded, and are never named in a
  notice or export.
* Minimised individual contributions are returned only with
  `knowledge:political:campaign-finance:individual-items:read`; otherwise
  they are counted per filing version. Natural-person payees of expenditures
  are reduced to their kind.
* The FEC's statutory limit on using contributor information
  (52 U.S.C. 30111(a)(4)) is recorded in the audit and in every OpenFEC
  source's licence note.

## Sources and coverage

Eight sources ship in `official-political-records` 1.4.0
(`config/source_packs/political.json`), each `unverified-live` and declaring
the minimisation policy; the selections are placeholders until the live
validation (#2529):

| Source | Provider | Records |
| --- | --- | --- |
| `us-fec-committees` | OpenFEC `/committee/{id}/history/` | committee registration per two-year period (Form 1 state as a dated revision) |
| `us-fec-candidates` | OpenFEC `/candidate/{id}/history/` | candidate registration per two-year period |
| `us-fec-filings` | OpenFEC `/committee/{id}/filings/` | every filing version with amendment indicator, chain, most-recent flag as published and totals as reported |
| `us-fec-schedule-a`, `us-fec-schedule-b` | OpenFEC schedules | itemised receipts and disbursements keyed by file number and `sub_id`; memo items labelled |
| `us-fec-schedule-e` | OpenFEC Schedule E | independent expenditures; 24/48-hour notices and periodic reports kept apart; support/oppose verbatim |
| `uk-ec-donations`, `uk-ec-spending` | Electoral Commission search exports | donations and spending items keyed by `ECRef`; corrections and late reports as revisions |

FEC bulk data is documented but not acquired (a whole-cycle file is not a
bounded selection).

## Journeys

1. **Committee to filings.** `campaign_finance_totals_as_of` returns each
   report's totals as of a date from the filing version received by then,
   naming it, with the amendment chain, the regulator's `most_recent` flag as
   published and the differences between versions.
   `campaign_finance_amendment_chain` and `campaign_finance_filing_items` show a
   single filing's versions and line items. A committee without filings is
   `none_on_record`.
2. **Reviewable identity.** `propose_campaign_finance_identity_matches` offers
   candidates into the shared identity state machine: official FEC committee
   ids, Companies House numbers, name+address in one country and
   name+office+cycle against elections records. A reviewer accepts or rejects
   (`review_campaign_finance_identity_match`) and can revert; nothing is
   automatic. Unmatched donors stay visible.
3. **Links by citation.** `link_campaign_finance_contests`,
   `link_campaign_finance_lobbying` and `link_campaign_finance_ownership` link
   specific filing and item revisions to contests, the lobbying register
   revision in force and Corporate Ownership records, recording the basis;
   missing providers and targets are reported.
4. **Organisation to donations.** `campaign_finance_affiliate_donations`
   expands an organisation through accepted matches, cited ownership relations
   and connected organisations stated on Form 1, listing the path for each
   contribution.
5. **Contest to filings.** `campaign_finance_contest_filings` lists candidate
   committee filings, party spending returns and independent expenditures
   (support/oppose as reported).
6. **Monitoring.** `create_campaign_finance_monitor` watches a committee, an
   organisation or a contest; `run_campaign_finance_monitor` evaluates it at a
   committed watermark and notifies new filings, amendments, terminations,
   most-recent flag changes and independent expenditures.

`export_campaign_finance_evidence_bundle` turns a totals, affiliate or contest
answer into an evidence bundle whose assertions cite the record revisions
behind them.

## Evidence

Offline evidence is the authored fixtures under
`tests/fixtures/campaign_finance/` and the acceptance journey
`tests/unit/domains/test_campaign_finance_acceptance.py`. Live evidence is
recorded separately under `docs/development/campaign-finance-evidence/` when
the dated live run (#2529) exists; none exists yet.
