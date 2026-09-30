# Political legislation: US Congress and UK Parliament bill dossiers

The Political pack's optional `legislation-us` and `legislation-uk` features
(tracking issue #2208) extend the existing legislative dossiers
(`src/domains/political/legislative_dossiers.py`, previously Bundestag and
EUR-Lex) to US federal bills and UK Parliament bills. A bill becomes a
dossier in the same store, citing committed revisions of the official records
behind it: text versions, actions and stages, sponsors, roll calls and
divisions, and Hansard debate references, each with its provider, revision and
observation time. Dossiers link to lobbying disclosures and, once enacted, to
Legal works by citation.

**Exclusions.** No passage prediction, no member scoring or ideology rating,
and no summary presented as a bill's legal effect. CRS summaries are stored
only as labelled CRS summaries; bill text is referenced by locator and hash,
never stored or re-summarised; Hansard contributions are references, never
transcripts. A lobbying disclosure naming a bill is not evidence of influence.

## Sources and coverage

The source audit (`docs/development/legislation-evidence/source-audit.md`)
records endpoints, API-key handling, licences (US public domain; UK Open
Parliament Licence v3.0 with its attribution statement), rate limits, revision
models and the bounded first coverage. Nine sources ship in
`official-political-records` 1.3.0 (`config/source_packs/political.json`),
each with `legislation.live_verification: unverified-live`:

| Source | Provider | Records |
| --- | --- | --- |
| `us-congress-gov-bills` | congress.gov API v3 | bill (`us-bill:<congress>-<type>-<number>`) with actions verbatim, sponsor, cosponsors (join and withdrawal dates), public-law citations, labelled CRS summaries |
| `us-congress-gov-house-votes` | congress.gov (House votes) | roll call with positions by bioguide ID |
| `us-senate-roll-calls` | senate.gov LIS XML | roll call with positions by LIS member id |
| `us-govinfo-bills` | GovInfo BILLS | text version: package id, version code, issue date, content hash, locator |
| `us-govinfo-billstatus` | GovInfo BILLSTATUS | the BILLSTATUS record, a separate source assertion |
| `uk-parliament-bills` | UK Bills API | bill (`uk-bill:<id>`, sessions beside it), sponsors, stages with sittings, publications |
| `uk-commons-divisions`, `uk-lords-divisions` | Commons / Lords Votes APIs | division lists per member id, tellers included |
| `uk-hansard-debates` | Hansard API | debate section references with contribution ids and locators |

US LDA quarterly reports (`us-senate-lda`, lobbying register `us-lda`) join
the existing lobbying feature so that disclosures naming a bill number can be
linked. API keys (`NOESIS_CONGRESS_GOV_API_KEY`, `NOESIS_GOVINFO_API_KEY`) are
sent as the `X-Api-Key` header and never appear in receipts or records.

## Journey

1. **Acquire** through the source-pack runtime (connector `legislation`,
   projector `noesis-legislation-record-v1` in `src/kb/legislation.py`). Each
   record is committed as an official-record document revision; a changed
   record is a new revision, an unchanged re-acquisition adds nothing and an
   older provider revision arriving late is logged without replacing the
   current one. Every unit leaves a receipt.
2. **Build the dossier** (`build_bill_dossier`): the bill's records become
   stages of one dossier in the existing store. Divisions and Hansard
   references name no bill themselves; the bill an operator declared them for
   is an unlinked review candidate until a reviewer accepts it
   (`review_bill_record_link`), after which the next dossier revision links it
   with `link_basis: reviewed_assertion`.
3. **Match members** (`propose_legislation_identity_matches`): bioguide, LIS
   and UK Parliament ids stay external identifiers; name-and-jurisdiction
   candidates to election candidates and scoped Political pack persons are
   reviewed, accepted or reverted through the shared identity state machine.
   Party, state and constituency are those each record stated at the vote or
   sponsorship date. Unmatched members stay visible.
4. **Link** lobbying disclosures (`link_bill_lobbying`) and enacted Legal works
   (`link_bill_enactment`) by published citation. Missing, ambiguous and
   Legal-unavailable targets are reported.
5. **Ask** (`bill_dossier_as_of`, `list_bill_votes`, `lookup_sponsor_bills`,
   `export_bill_evidence_bundle`): the stage and text version in force on a
   date, sponsors, member positions with accepted matches only, congress.gov
   vs BILLSTATUS disagreements shown unresolved, lobbying and enactment links,
   and an evidence bundle citing every revision. A bill without records is
   `none_on_record`.
6. **Monitor** (`create_legislation_monitor`, `run_legislation_monitor`):
   subscriptions on a bill or a sponsor notify new actions, cited laws, text
   versions, stages, Royal Assent, votes and corrected divisions, citing the
   new and previous revision. Live revisions from unverified providers are
   withheld.

## Evidence

Offline evidence is the fixture-driven suite
`tests/unit/domains/test_legislation_*.py`; the end-to-end journey is
`tests/unit/domains/test_legislation_acceptance.py`. No dated live run exists
yet (LT13, #2458); live evidence will be recorded separately under
`docs/development/legislation-evidence/`.
