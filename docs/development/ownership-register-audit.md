# Corporate Ownership: further registers audit (#2885, #2886, #2887)

Audited 2026-11-02 for the Corporate Ownership pack
(`docs/roadmaps/corporate-ownership-pack-scope.md`). The machine-readable
outcome is `PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in
`src/ingestion/ownership_providers.py`; this page records how each decision was
reached. Items marked **verify** could not be confirmed from a primary source:
from this runtime only web search was available, and the Companies House,
GOV.UK, HM Land Registry, OpenCorporates and Open Ownership hosts did not
resolve (`download.companieshouse.gov.uk`, `forum.companieshouse.gov.uk` and
`www.gov.uk` were tried). Licence terms are not paraphrased beyond what the
cited pages say. Nothing here was acquired live: every fixture is authored and
fictional, and every new source stays `unverified-live`.

## Summary

| Source | Decision | Why |
|---|---|---|
| Companies House PSC snapshot | implemented: bulk adapter `companies-house-psc-snapshot` with the ownership sink | free daily bulk product; bounded by declared company numbers or a company-number prefix; same records as the API path |
| Register of Overseas Entities (Companies House) | implemented: source `companies-house-roe` (existing `companies-house` connector with OE numbers) | the public data API serves overseas entities, their managing officers and registrable beneficial owners |
| HM Land Registry OCOD | not implemented | account plus per-account licence acceptance; no title numbers on the `geospatial.real-estate` side to join on |
| BODS: Denmark CVR | implemented: source `open-ownership-bods-dk` | maintained by Open Ownership's BODS data service |
| BODS: Slovakia RPVS | implemented: source `open-ownership-bods-sk` | maintained by Open Ownership's BODS data service |
| BODS: Latvia | not implemented | published by the register itself; statement source description and BODS version unverified |
| OpenCorporates | implemented: connector `opencorporates`, source `opencorporates-companies`, key `NOESIS_OPENCORPORATES_API_KEY` | aggregator for identity and officers of explicitly selected registrations |

## #2885 Companies House People with significant control snapshot

**Product.** Companies House publishes a *People with significant control
(PSC) snapshot* on its free data products page
(`https://download.companieshouse.gov.uk/en_pscdata.html`): a JSON data file
with the full list of PSCs provided to Companies House, as one file or split
into smaller ZIP files (the page has listed 31 parts). The data.gov.uk listing
says the snapshot is provided free of charge, is not supported, and is updated
every morning before 10am GMT. Secondary sources describe the content as JSON
lines `{"company_number": ..., "data": {...}}` with a closing `totals#...` line;
the adapter accepts split parts `psc-snapshot-YYYY-MM-DD_NofM.zip` and a single
`persons-with-significant-control-snapshot-YYYY-MM-DD.zip` (**verify** the
current file names and line format against a live release). Community forum
threads report occasional delays and broken links, so a failed listing fails the
run receipt rather than reporting zero records.

**Licence and reuse (verify).** The GOV.UK *Companies House data products*
guidance points to Companies House's public task and Crown copyright statement
and its personal information charter. It does not name a licence for the
snapshot. data.gov.uk lists the licence as "TBD". Sources conflict:

- a Companies House disclosure-log answer (around 2017) says the agency does
  not make its public information, including the Free Data Product, available
  under the Open Government Licence;
- a 2026 third-party blog post describes the API and the PSC files as OGL v3.

The contract therefore records the Crown copyright / public task statement as
the reuse basis and marks the OGL status **conflicting, verify**. It does not
claim OGL.

**Personal-data assessment.** PSC entries name natural persons (name, month
and year of birth, nationality, country of residence, service address, natures
of control). They are published under the Companies Act 2006, but reuse of
personal data stays subject to UK GDPR. Decisions:

- Default: an individual PSC becomes an owner-scoped `person` record (name,
  nationality, country of residence) and owner-scoped assertions. Other
  principals see only that a person-level statement exists (redacted), and
  ownership reviewers see it in full. This is how BODS person statements are
  already handled.
- Person-research mode (`NOESIS_PERSON_RESEARCH`,
  `docs/security/person-research-mode.md`): stored unscoped, with the published
  month and year of birth, and marked with the policy
  `ownership-person-research-v1`. Writing such a record with the switch off is
  refused (`minimisation_violation`).
- Never stored: service or residential addresses, and the full date of birth
  (the snapshot does not publish one).
- Super-secure PSCs (details withheld by the register) are kept as a
  `reporting_exception`. They are never treated as "no owner".

**Implementation.** `src/ingestion/bulk/adapters/companies_house_psc.py` (bulk
runner) feeds the existing parsers through
`src/ingestion/ownership_psc_snapshot.py`. The ownership sink
(`src/ingestion/bulk/ownership_sink.py`, CLI `--sink ownership`) writes the
records to `OwnershipStore`. The rules:

- **Selection:** `company_numbers` and/or a two-letter `company_prefix` (for
  example `OE` for overseas entities). The whole register is never kept.
- **Same statements as the API:** natures, bands, validity and person handling
  are identical. Statement and holder keys are namespaced
  `companies-house:psc-snapshot...`, so each channel keeps its own revision
  chain. Company identity (`companies-house:gb-coh:<number>`) is shared, so
  corporate PSCs resolve into the ownership graph by company number. England and
  Wales numbers stated without leading zeros are padded to eight characters.
- **Revisions:** a record's source hash is the hash of its canonical snapshot
  line, never the file or the position. An unchanged entry in a later snapshot
  is not a revision, and a ceased or changed entry is one. An entry missing from
  a later snapshot is never deleted. Re-running a processed release is skipped
  by the runner (`already-done`). Re-acquiring the same snapshot from scratch
  changes no record.
- **Unrecognised lines** are counted in `unrecognised_lines`. They are never
  guessed.

`GB Persons Of Significant Control Register` statements republished as BODS by
Open Ownership were already implemented (source `open-ownership-bods`). The
issue text described that publisher as `not-implemented`, but in the code the
not-implemented entries were Denmark and Slovakia (see #2887).

## #2886 Register of Overseas Entities

**Product.** Since 2023 overseas entities that own UK land register with
Companies House and receive OE-prefixed numbers. The Companies House public
data API serves them through the usual company resources:

- the profile has `type: registered-overseas-entity` and
  `foreign_company_details`;
- managing officers come from `/officers` (`managing-officer`,
  `corporate-managing-officer`);
- registrable beneficial owners come from `/persons-with-significant-control`
  with the kinds `individual-beneficial-owner`,
  `corporate-entity-beneficial-owner`, `legal-person-beneficial-owner` (for
  example government bodies) and `super-secure-beneficial-owner`. Their natures
  of control end in `-registered-overseas-entity`, and trust-held ones in
  `-as-trust-registered-overseas-entity`.

The developer specifications document the `corporate-entity-beneficial-owner`
and `individual-beneficial-owner` resources, including an optional
`is_sanctioned` flag "declared as being sanctioned on the latest filing". The
other kinds and the nature list come from Companies House forum threads
(**verify** against the API enumeration files). Beneficial-owner statement codes
for overseas entities were not found. The fixture uses an authored placeholder
code, and the parser keeps any code verbatim.

**Terms.** These are the same as the `companies-house` API source: the API key
`NOESIS_COMPANIES_HOUSE_API_KEY` and the Companies House public data terms.
Trust details are not on the public register (since 2024 they are available
only on application, **verify**), so they are not acquired. Only what the
natures state ("held as trust") is kept.

**Mapping.**

- The overseas entity's jurisdiction is the stated originating registry's
  country (for example Jersey → `JE`), else unknown. It is never `GB`. Every
  unmapped profile field is kept verbatim under `native.overseas_entity`, so any
  published verification or status field survives as published.
- Individual beneficial owners follow person-research mode, as described for
  #2885.
- A corporate beneficial owner registered in England and Wales resolves to its
  company number.
- A government body becomes a `legal_entity`.
- `is_sanctioned` is kept as `is_sanctioned_as_declared`. It is never a Noesis
  determination.
- Super-secure owners are a `reporting_exception`.
- Exempt and unverified statuses are kept as published.
- The ROE's "more than 25 percent" band does not settle majority or minority.
  The graph reports it as `band-spans-majority` and keeps it in the parent slot.

**HM Land Registry OCOD: not implemented.** *Overseas companies that own
property in England and Wales* (formerly OCOD) is listed by HM Land Registry
among its datasets that are free under licence. Access is through the *Use land
and property data* service: according to secondary sources, an account,
acceptance of the dataset licence, and then an API key (**verify**). It carries
title numbers, which link to other HM Land Registry datasets. It is not
implemented for two reasons:

1. An operator account and a per-account licence acceptance are required.
   Noesis cannot give that acceptance on the operator's behalf.
2. `geospatial.real-estate` records carry no title numbers. They hold INSPIRE
   ids and Price Paid transactions only
   (`src/ingestion/real_estate_sources.py`), so there is no shared published
   identifier to join on. Spatial joins are excluded by design.

Overseas-entity dossiers therefore list beneficial owners and managing
officers, but no UK titles.

## #2887 Further BODS publishers and OpenCorporates

**BODS publishers.** Open Ownership's BODS data service
(`bods-data.openownership.org`) offers current UK, Denmark and Slovakia
beneficial-ownership data for download and reuse. Its 2023 guidance says all
datasets are published under an open licence, without naming one. Its appendix
lists Denmark's CVR and Slovakia's RPVS as transformed to BODS 0.2 under
`/source/denmark/` and `/source/slovakia/`. A GLEIF dataset at BODS 0.4
(2025-03-11) and a UK 0.4 dataset also exist; those are not used here, because
the mapper handles 0.2/0.3.

- **Denmark CVR**: implemented. Licence per statement
  (`publicationDetails.license`). The CVR data are reused under the Danish
  Business Authority's terms for free public data (**verify**).
- **Slovakia RPVS** (register of public sector partners): implemented. Public
  by statute (Act No. 315/2016 Coll.; reuse terms **verify**).
- **Latvia**: not implemented. The Register of Enterprises publishes its own
  BODS release on data.gov.lv (CC0 according to secondary sources, **verify**),
  but the statement `source.description` and the BODS version of the current
  files are unverified, so no statement can be attributed to it safely.

Each publisher is acquired by its own source, through the new
`ownership.publishers` selection. A source projects only statements whose
register source it declares, and counts the others as `not_selected`. Each
publisher therefore keeps its own statements and revision chains. Conflicting
statements from two registers about one company coexist and are returned
together, never resolved. Person statements follow person-research mode.

*Personal-data note:* after the CJEU judgment of 22 November 2022 (WM and Sovim
SA, C-37/20 and C-601/20), several EU registers restricted public access, and
AMLD6 moves access to a legitimate-interest basis. A publisher's availability or
terms can therefore change, which is a reason to re-audit before any live run
(**verify**).

**OpenCorporates.** API v0.4 (`/companies/{jurisdiction_code}/{company_number}`)
is implemented as the `opencorporates` connector with the key
`NOESIS_OPENCORPORATES_API_KEY`, sent as `api_token` and never stored.
OpenCorporates describes its data as open data under ODbL 1.0 with attribution.
Free share-alike keys are available for public-benefit users, and other uses
need a paid plan (see `docs/guides/connector-api-keys.md`; **verify** against
the key's plan). It is an aggregator, never an authoritative substitute for a
register:

- every record names OpenCorporates as publisher and links the company page;
- the national register number is added as an identifier only for configured
  jurisdictions (`gb` → `gb-coh`, `dk` → `dk-cvr`);
- identity review proposes OpenCorporates matches as
  `cross-referenced-identifier`, never `exact-identifier`.

Search and document enrichment stay with the regional provider (closed issue
#1483).

**Cross-jurisdiction chains.** An ownership chain across registers resolves
only through reviewed identity decisions. For example, a Slovak company is held
by a Danish company (RPVS), which is held by a UK company (CVR), which is a
Companies House company. The chain joins only after a reviewer accepts the
DK-CVR and GB-COH candidates, and reverting a decision splits it again.

## Sources consulted (web search, 2026-11-02)

- data.gov.uk: People with significant control (PSC) dataset listing; Free Company Data Product listing.
- GOV.UK: Companies House data products guidance; HM Land Registry public data.
- Companies House forum: "People with significant control (PSC) snapshot has been stopped"; "UBO API - data returned"; "GDPR and ICO considerations".
- Companies House disclosure log 362 (Free Company Data Product enquiry).
- Companies House developer specifications: corporateentitybeneficialowner, individualbeneficialowner, legalpersonbeneficialowner.
- Open Ownership: "Analyse and reuse beneficial ownership data from the UK, Denmark or Slovakia"; "Developing the world's first transnational open beneficial ownership register" (appendix); BODS data analysis tools guidance (2023-07).
- OpenCorporates: pricing and terms of use (via `docs/guides/connector-api-keys.md`).
- powerbase.info OCOD summary; API Evangelist HM Land Registry listing (secondary).
