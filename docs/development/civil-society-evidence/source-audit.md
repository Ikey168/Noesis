# Civil society and nonprofits: source-contract audit and bounded coverage (CV01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once an
audit names a surviving source; this audit names four, subject to the live
check.

This audit sets out, per source, what the Society bundle's proposed
`society.civil-society` provider (subdomain `civil-society`, ADR-005) may
acquire, how, on what terms, and how the personal data of trustees, officers
and grant recipients is minimised. The home is the existing `packs/society/`
bundle (today carrying `society.income`); the roadmap row still says "new
bundle" because it predates that bundle. **It was written without network
access: the publishers' hosts (`www.irs.gov`, `apps.irs.gov`,
`register-of-charities.charitycommission.gov.uk`,
`api.charitycommission.gov.uk`, `api.threesixtygiving.org`) were blocked by
this runtime's egress proxy (HTTP 403 on CONNECT, verified 2026-10-03), so
terms, endpoints, parameters and field names were not re-verified live.**
Every item marked _verify_ must be checked against the live documentation, the
live terms and a real response before the first dated live run (the track's
"Validate live coverage" issue, not yet opened). No source is `live` until
that run exists.

**The machine-readable copy does not exist yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `MINIMISATION` and `LIVE_VERIFICATION` will be added in
`src/ingestion/civil_society_sources.py` by the track's acquisition issues and
must match this audit.

Non-goals for every source: no charity quality, efficiency, overhead or
impact ratings; no "programme spend ratio" or compensation benchmark presented
as a judgement; no inference of political activity, fraud or undue influence
from co-occurring records; no profiling of trustees, officers, donors or
individual grant recipients. Figures are kept as each filer or register
published them; an amended return never overwrites the return it amends.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `us-irs-eo-bmf` | US Internal Revenue Service, Exempt Organizations Business Master File extract | per EIN: name, subsection and classification codes, ruling date, deductibility, foundation code, status, NTEE code, latest asset, income and revenue amounts and filing requirement | CSV files per state or region on `www.irs.gov`, no key | `unverified-live` |
| `us-irs-990-efile` | IRS, e-filed Form 990 series (TEOS XML) | one return per `OBJECT_ID`: header, financial summary, Part VII officers, directors, trustees and key employees with compensation, Schedule I grants to organisations, Schedule R related organisations, Schedule C lobbying totals | yearly index CSV plus yearly ZIP archives of XML returns on `apps.irs.gov`, no key | `unverified-live`, **conditional on a bounded read** (below) |
| `uk-charity-commission-register` | Charity Commission for England and Wales, Register of Charities | per registered charity and linked charity: registration and removal, type, company number, classification, areas of operation, trustees, five-year financial history, annual-return history | Register of Charities API (JSON), subscription key | `unverified-live` |
| `uk-360giving-datastore` | 360Giving, Datastore API | grants made and received per organisation identifier (org-id), as published by funders in the 360Giving Data Standard | anonymous JSON API | `unverified-live` |

Documented, not acquired:

- **Charity Commission full register extract** (daily ZIP of JSON or text
  tables, _verify_): the whole register is not a bounded selection; reserved
  for the live cross-check.
- **IRS auto-revocation list, Pub. 78 data and Form 990-N e-Postcard**: declared
  as next documents. The auto-revocation list is the IRS's own statement of
  revocation; until it is acquired, an EIN missing from a later BMF extract is
  only an observed absence.
- **Form 990-EZ and 990-PF**: declared as next documents; the first coverage
  parses Form 990 only.
- **ProPublica Nonprofit Explorer API and Candid (GuideStar)**: secondary
  re-publishers of IRS data; the primary source is acquired, and Candid's
  data is commercially licensed. ProPublica's reuse terms are _verify_.
- **GrantNav**: the web interface to the same 360Giving data.
- **German subject**: no open national register of tax-privileged bodies is
  known to the author; the status of the BZSt Zuwendungsempfängerregister is
  _verify_ for a later version of this audit. OSCR (Scotland) and the Charity
  Commission for Northern Ireland are not audited here.

**Form 990 bounded read.** IRS publishes e-filed returns as yearly ZIP
archives of hundreds of megabytes each (_verify_ sizes), with an index CSV
naming each return's `OBJECT_ID`, EIN, tax period and archive. A whole archive
is not a bounded acquisition. The adapter reads the index, then fetches only
the declared return's member using HTTP range requests on the ZIP central
directory and the member (_verify_ that the host honours `Range`). If the host
does not, the source moves to `blocked` with the reason "no bounded access",
the subdomain stays covered by the other sources, and no archive is
downloaded whole as a workaround.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| EO BMF | `https://www.irs.gov/pub/irs-soi/eo_{state}.csv` (state files) and `eo1.csv`-`eo4.csv` (regions) (_verify_ paths); columns `EIN`, `NAME`, `ICO`, `STREET`, `CITY`, `STATE`, `ZIP`, `SUBSECTION`, `CLASSIFICATION`, `RULING`, `DEDUCTIBILITY`, `FOUNDATION`, `STATUS`, `TAX_PERIOD`, `ASSET_AMT`, `INCOME_AMT`, `REVENUE_AMT`, `NTEE_CD` (_verify_) | none | US government work, public domain (_verify_ the irs.gov notice); cite the IRS and the extract month | undocumented (_verify_); one file per unit; a file over 60 MB is `budget_exhausted` | refreshed monthly (_verify_); no row stamp, so the extract month is the vintage and a changed row is a new revision keyed by EIN; an EIN absent from a later extract is a `not-returned` observation, never a stated revocation; rows of undeclared EINs are counted as out of selection in the receipt and not stored |
| Form 990 e-file | `https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv` and `.../{year}/{year}_TEOS_XML_{nn}{A-D}.zip` (_verify_ both); XML elements per IRS e-file schema, e.g. `ReturnHeader/Filer/EIN`, `TaxPeriodEndDt`, `AmendedReturnInd`, `CYTotalRevenueAmt`, `Form990PartVIISectionAGrp/PersonNm`, `TitleTxt`, `ReportableCompFromOrgAmt`, `RecipientTable` (_verify_ each, per `returnVersion`) | none | public domain; returns are public under IRC § 6104 (_verify_); cite the return by EIN, tax period and `OBJECT_ID` | undocumented (_verify_); at most 10 returns per run, 5 MB per return | each return is immutable and its own record; an amended return (`AmendedReturnInd`) is a new filing version for the same EIN and tax period and never overwrites the original; IRS states no amendment chain, so versions are listed side by side by submission date, never reconciled; an unknown `returnVersion` is `schema_drift` |
| Charity Commission | `https://api.charitycommission.gov.uk/register/api/allcharitydetailsV2/{registeredNumber}/{suffix}`, `.../charityfinancialhistory/...`, `.../charitytrusteenames...` (_verify_ every path and version suffix); subsidiary (linked) charities by suffix | subscription key from the Commission's API developer portal (free registration, _verify_), required secret `NOESIS_CHARITY_COMMISSION_API_KEY`, sent as the `Ocp-Apim-Subscription-Key` header (_verify_); never in a URL, receipt or record | Open Government Licence v3.0, attribution "Contains public sector information licensed under the Open Government Licence v3.0" (_verify_ that the API terms carry the OGL and impose no further condition) | per-subscription quota (_verify_); HTTP 429 is `rate_limited` with `Retry-After` | the register is updated continuously (_verify_); no record stamp, so the payload digest decides; a changed entry is a new revision; a removal is a stated lifecycle revision (`date_of_removal`, `removal_reason`, _verify_), never a deletion; a restated financial year is a new revision of that year's figures, the earlier kept |
| 360Giving Datastore | `https://api.threesixtygiving.org/api/v1/org/{org_id}/grants_made/` and `/grants_received/`, `limit`/`offset` with `next` (_verify_); grant fields per the 360Giving Data Standard (`id`, `title`, `amountAwarded`, `currency`, `awardDate`, `recipientOrganization`, `fundingOrganization`, `plannedDates`, `recipientIndividual`) (_verify_ standard version) | none (_verify_) | each grant is under its publisher's dataset licence (CC BY 4.0, CC0 or OGL v3.0 expected; _verify_ how the API exposes it, else read the 360Giving registry); a grant whose licence is missing or not open is not stored and is counted in the receipt | undocumented (_verify_); at most 10 pages of 100 per unit | the Datastore reloads publishers' files (cadence _verify_); a changed grant is a new revision keyed by grant `id`; a grant no longer returned is a `not-returned` observation (a publisher may withdraw a file); the Datastore's load date is stored as `data_as_of` |

**Unavailable-access fallback.** A failed unit (HTTP error, a missing key, a
redirect to another host, schema drift, a response over its budget) fails that
source's run with its code and a receipt; earlier revisions stay current and
nothing is marked removed or revised because of a failure. Without
`NOESIS_CHARITY_COMMISSION_API_KEY` the register source fails with
`authentication_failed` and readiness reports it as `unavailable`.

## Data minimisation decision

Trustees, officers, directors and key employees are natural persons published
by law to make an organisation's governance accountable; they are not public
office holders. Individual grant recipients and donors are private persons.
Policy `civil-society-minimisation-v1`, enforced in the parser and again at
write time (`minimisation_violation` before anything is written), following
the officer pattern of `src/kb/ownership_records.py` (`officer_role`: name,
key, kind, role, dates) and the donor rule of the campaign-finance audit:

- **Stored for an organisation:** identifiers (EIN, registered charity number
  and suffix, company number, org-id), names, codes, dates, city and state or
  country, figures as filed, grants made and received to and from
  organisations.
- **Stored for a person in a governance role:** name as published, role or
  title, the organisation, appointment date and chair flag as published, and
  for Form 990 Part VII the average hours and reportable compensation amounts
  as filed. Returned only to principals holding
  `knowledge:civil-society:persons:read` in addition to the read scope;
  everyone else sees per organisation the count of persons by role and the
  totals the filing itself reports.
- **Redacted at acquisition:** the BMF `ICO` (in-care-of name) and street and
  ZIP (a small organisation's address is often a person's home); the Form 990
  paid-preparer name and PTIN and the signing officer's phone; Part VII
  Section B independent-contractor names (count and amounts kept).
- **Excluded:** Schedule B contributor data in any form; grants to individuals
  (360Giving `recipientIndividual`, Schedule I Part III rows beyond the
  aggregate totals the form reports), counted only; trustee or officer
  addresses, dates of birth and other appointments.
- **Never matched:** persons are excluded from identity proposals and links;
  only organisations are proposed, by stated identifiers (EIN, charity number,
  company number, org-id) first and names only as weak candidates.
- **Retention:** revisions are retained with their source run for provenance.
  If a register later withholds a person it published (a trustee name
  dispensation, _verify_ how the Commission marks it) or removes a grant, the
  next acquisition records that as a revision and an audited minimisation
  revision removes the person's name from earlier revisions; the source's
  withholding is never filled from another source.
- **Who may query:** `knowledge:civil-society:read` with namespace access;
  writes need `knowledge:civil-society:write`, identity and link reviews
  `knowledge:civil-society:review`.

## Record shapes and reuse

Shapes from `packs/taxonomy.json`: **registry-records** (BMF and register
entries, with lifecycle revisions) and **versioned-documents** (Form 990
returns and their amended versions). No new shape. Grants are projected as the
`award` kind of `noesis-funding-record-v1` (`src/kb/funding_records.py`)
rather than a new grant shape; the acquisition issue confirms this or records
why not.

Reuse: the store pattern of `src/kb/income_distribution_store.py` in the same
bundle; organisation identity through `src/kb/ownership_identity.py` and
`src/kb/entity_history.py` (charity company numbers to Companies House
records acquired by `src/ingestion/ownership_providers.py`; EINs as stated
identifiers); lobbying links through `src/kb/lobbying_identity.py` (a
registrant that is a charity) with Schedule C totals cited, never read as
influence; funding links through `src/kb/funding_records.py`. Every link is
a reviewable candidate citing the revisions it rests on.

## Bounded first coverage

| Source | Selection | Periods | Caps |
| --- | --- | --- | --- |
| EO BMF | declared EINs from one state file | the current extract | 25 EINs, 1 file of at most 60 MB |
| Form 990 | two declared public charities among those EINs, every original and amended return | two tax years | 10 returns, 5 MB per return, 200 Part VII rows, 500 Schedule I rows |
| Charity Commission | declared registered charities: one with linked (subsidiary) charities, one removed charity, one charitable company | current entry and five-year history | 10 charities, 50 trustees per charity |
| 360Giving | grants made by one declared funder (org-id) whose grants reach the declared charities, and grants received by those charities | as returned | 1,000 grants made, 500 received per charity |

Justification: one US and one UK subject show a register entry, its filings
and grants side by side without merging them; the removed charity and an
amended return show lifecycle and amendment revisions; 360Giving org-ids
give the stated-identifier links to the register and to Companies House. A
German subject is deferred until a source is audited. Every further
organisation, form or funder is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| EO BMF | `unverified-live` | - | none; offline fixtures not yet authored |
| Form 990 e-file | `unverified-live` | - | none; range-read support unverified |
| Charity Commission | `unverified-live` | - | none; no key held |
| 360Giving Datastore | `unverified-live` | - | none; offline fixtures not yet authored |

The fixtures, once written, will be authored, not captured: fictional
organisations, EINs, charity numbers and persons, tax periods in 2094-2097
and extract or load dates in 2098-2099, so nothing can be mistaken for a
published record.
