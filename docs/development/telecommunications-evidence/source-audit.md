# Telecommunications and spectrum: source-contract audit and bounded coverage (TC01)

Tracking: issue #2819 (TC01) · wave 3 tracker #2814 · recorded 2026-11-01.

This audit sets out, per candidate, what the Technology bundle's proposed
`technology.telecommunications` provider (subdomain
`telecommunications-spectrum`, domain Technology, ADR-005) may acquire, how,
and on what terms. No per-track tracker or delivery issue exists yet; the
decision section says whether one should open. **It was written without
network access: the publisher hosts (`www.fcc.gov`, `data.fcc.gov`,
`wireless2.fcc.gov`, `broadbandmap.fcc.gov`, `www.bundesnetzagentur.de`,
`www.govdata.de`, `datahub.itu.int`, `www.itu.int`) are blocked by this
runtime's egress proxy (CONNECT refused with 403, verified 2026-11-01), so
terms, endpoints, file layouts, record types and rate limits were not
re-verified live.** They come from the issue's candidate list and the
publishers' documentation as the author knows it. Every item marked _verify_
must be checked against the live pages, the live terms and a real response
before the track's first dated live run (its "Validate live coverage" issue,
not yet opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`,
`EXCLUSIONS` and `LIVE_VERIFICATION` would be added in
`src/ingestion/telecommunications_sources.py` by the track's acquisition
issues and must match this audit; a difference is resolved by changing this
audit first.

Non-goals for every source: no coverage modelling or availability estimates
of our own, no interference, propagation or link-budget analysis, no
person-level licensee records, no location-level (address or broadband fabric)
data, no market-share or concentration figures computed by Noesis, no ranking
of operators, and no blending of FCC, Bundesnetzagentur and ITU figures into
one series. Values and records are stored as each publisher released them,
side by side.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `telecom-fcc-uls-licences` | US Federal Communications Commission, Universal Licensing System (ULS) | licence records per call sign: radio service, status, grant, effective and expiry dates, market or location, frequencies, licensee entity name and FCC Registration Number (FRN) | ULS public bulk files per radio service (zipped pipe-delimited `.dat` record types such as `HD`, `EN`, `MK`, `FR`, _verify_) and the License View API (`data.fcc.gov/api/license-view/...`, JSON, _verify_ it is still served) | `unverified-live`, for licences held by non-individual licensees in declared market-based services only (see "Data minimisation") |
| `telecom-fcc-bdc-summary` | FCC, Broadband Data Collection (National Broadband Map) | published availability summaries by geography (state, county, _verify_ levels), technology and speed tier, per BDC as-of date | summary data downloads from the National Broadband Map data download page (_verify_ that summary files are downloadable without an account) | `unverified-live`, **conditional**: kept only if the summary files download without an account; otherwise `gated-not-granted` like the API |
| FCC BDC public data API | FCC | the same downloads, listable by as-of date | API requiring a registered username and an API token (`hash_value`, _verify_) | `gated-not-granted`: no account or token is held and none was requested; the summary files cover first coverage without it |
| FCC BDC location-level availability and the Broadband Serviceable Location Fabric | FCC; the Fabric is produced under licence by a contractor (CostQuest Associates, _verify_) | provider availability per location id; the location records themselves | location-level files by state; the Fabric only under a licence agreement (_verify_) | `not-implemented`: location-level data are out of scope (non-goals), and the Fabric's licence restricts use and redistribution (_verify_) |
| Bundesnetzagentur | Bundesnetzagentur für Elektrizität, Gas, Telekommunikation, Post und Eisenbahnen | frequency plan, frequency assignments and auction results, telecommunications market data (annual and activity reports) | web pages and PDF reports; interactive portals (for example the EMF data portal and the Gigabit-Grundbuch, _verify_); no documented machine API or bulk telecom dataset known to the author (_verify_ GovData listings) | `not-implemented`: no stable machine access known; PDF reports and interactive portals are never scraped. Reopening needs an amendment naming a published file or API and its licence (for example Datenlizenz Deutschland, _verify_) |
| ITU DataHub | International Telecommunication Union | ICT indicators per economy and year (subscriptions, internet use, coverage, prices) | interactive DataHub with downloads (_verify_); the underlying World Telecommunication/ICT Indicators Database is distributed under a paid or subscription licence (_verify_) | `not-implemented`: the ITU terms are understood to restrict redistribution of the indicators database (_verify_), and DataHub's reuse terms and any API are undocumented to the author (_verify_) |

**ITU indicators through the UN SDG database.** Some ITU indicators also
reach the UN SDG Global Database as SDG indicators (for example 9.c.1 mobile
network coverage, 17.6.1 fixed broadband subscriptions, 17.8.1 internet use,
_verify_ numbering) and could be read through the UNSD SDG API instead
(_verify_). That route is a different source with its own terms and is not
audited here; adding it needs an amendment to this audit and a source-pack
version bump, as the tourism audit recorded for UN Tourism.

**Unavailable-access fallback.** A failed request or file (HTTP error, 429
after one retry, redirect to an undeclared host, a `.dat` record type or
column count different from the declared layout, a file over the byte budget)
fails that source's run with its code and a receipt; earlier revisions and
vintages stay current and nothing is marked removed because of a failure.
Readiness reports the source as `stale`. Queries for Bundesnetzagentur or ITU
report `not-implemented`, never empty.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| FCC ULS (`telecom-fcc-uls-licences`) | License View API: `https://data.fcc.gov/api/license-view/basicSearch/getLicenses?searchValue=<FRN or call sign>&format=json` (_verify_); bulk: weekly complete files per service (`https://data.fcc.gov/download/pub/uls/complete/l_<service>.zip`, _verify_) and daily transaction files (_verify_) | none | FCC data are works of the US government and understood to carry no copyright restriction (_verify_ the FCC website's terms and any disclaimer on ULS reuse) | none published (_verify_); paced at 1 request/second; one weekly complete file per declared service per run | each licence is keyed by call sign (and ULS unique system identifier, _verify_); a changed status, expiry, assignment or licensee on a later file is a new revision; an assignment or transfer of control is recorded as stated with its file number; a call sign absent from a later complete file is a `removed_by_source` revision only after a successful full-file run; cancelled and expired licences stay recorded with their status |
| FCC BDC summary (`telecom-fcc-bdc-summary`) | summary file links from `https://broadbandmap.fcc.gov/data-download` per as-of date (_verify_ path and naming) | none (conditional, _verify_) | as FCC data above (_verify_); the Fabric-derived counts in summaries may carry a licence note (_verify_) | none published (_verify_); one file per declared as-of date and geography level | each BDC filing period has an as-of date (June and December, _verify_); the FCC republishes a period's data as challenges and corrections are processed, so a changed file for the same as-of date is a new vintage of that period, never an overwrite; summary values carry the release (version) date the FCC states (_verify_) |
| FCC BDC API, location data, Fabric | none acquired | API token (not held); Fabric licence (not held) | Fabric licence restricts use (_verify_) | - | - |
| Bundesnetzagentur | none acquired | - | Datenlizenz Deutschland for GovData datasets where published (_verify_) | - | - |
| ITU DataHub | none acquired | - | ITU terms, understood as restricting redistribution of the indicators database (_verify_) | - | - |

## Record shapes and reuse

Record shapes (`packs/taxonomy.json`): **registry-records** for ULS licences
(entries in a published register with revisions and reviewable licensee
identity), and **statistical-series** for BDC availability summaries
(numeric series per geography, technology and speed tier, each as-of release
a vintage). No new shape is needed; `technology.internet-infrastructure` and
`technology.ai-models` already pair these shapes in the Technology bundle.

- BDC summaries reuse the economic series storage (`register_series` in
  `src/domains/economic/model.py`), as the statistics providers do; county
  and state geographies are keyed by published FIPS codes.
- ULS licensee organisations are reviewable identities keyed by FRN; an FRN
  is never matched to an ASN, a company register entry or an
  internet-infrastructure organisation by name. A link to
  `technology.internet-infrastructure`, `ownership.competition` or
  `geospatial.infrastructure` exists only where a reviewer confirms an
  identity, and is shown with both citations.

## Data minimisation decision

ULS licence records can name natural persons: individual licensees (amateur
radio, GMRS, many ship, aircraft and commercial operator licences) and contact
persons of company licensees, with postal addresses, telephone numbers and
e-mail addresses. Decision:

- **Stored:** call sign, radio service, status, dates, market or location
  designator, frequencies and emission designators as published, licensee
  entity name and FRN **when the licensee is not an individual**; BDC
  published aggregates per geography, technology and speed tier.
- **Redacted:** for company licensees, the `EN` record's contact person name,
  street address, telephone, fax and e-mail fields are never read or stored;
  only entity name, FRN, entity type and country are kept.
- **Excluded:**
  - whole radio services licensed mainly to individuals: Amateur (`HA`, `HV`),
    GMRS (`ZA`), and personal ship, aircraft and commercial operator licences
    (`SA`, `AC`, `CO` and similar, _verify_ the service codes); their bulk
    files are never downloaded;
  - any licence, in any service, whose `EN` record states an individual
    applicant type (`I`, _verify_ the code and field) or that has no entity
    name: the record is dropped before storage, and only a count of dropped
    records is kept in the run receipt;
  - antenna structure registrations, application and pleading attachments,
    and all daily application files (`a_*`) in first coverage;
  - BDC location-level availability, the Fabric, provider-by-location files and
    challenge data; any address or location id.
- **Retention:** licence revisions and series vintages are kept for
  provenance; since no stored field identifies a person, no erasure workflow
  applies. If a stored company entity is later found to be a sole trader
  under a personal name, its records are removed on review.
- **Who may query:** `knowledge:technical:telecommunications:read` with
  namespace access; writes `...:write`; identity reviews `...:review`, as the
  Technology providers use them (_verify_ scope names against the bundle).
- **Query rule:** lookups are by call sign, FRN of a declared company,
  service and market only; no search by person name or address is offered.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| FCC ULS | licences held by two declared company FRNs in one declared market-based service (for example AWS-3 or 600 MHz, _verify_ service code), through the License View API | 2 FRNs, 1 service, 200 licences, 4 requests |
| FCC BDC summary | one declared as-of date; one declared state and its counties; fixed and mobile summary tables | 2 files, 20 MB each, 500 series |

Justification: two operators' licences in one service show licence status,
renewal and an assignment revision without touching any individual licensee;
one state's summaries show a BDC period's vintages and county breakdowns
without location data. Every further operator, service, as-of date or state
is a source-pack version bump. The bulk weekly files are used only if the
License View API is no longer served (_verify_), and then only for the one
declared service.

## Open questions, answered

- **Are licensee records company-only, and how are individual licensees
  excluded?** Stored licensee records are company-only. Individuals are
  excluded twice: whole services licensed mainly to individuals (amateur
  radio, GMRS, personal ship, aircraft and operator licences) are never
  acquired, and in every other service a record whose `EN` applicant type is
  individual, or that has no entity name, is dropped before storage. Company
  contact-person fields are never read.
- **Can broadband availability be bounded to published aggregates rather than
  location-level fabric data?** Yes. Only the FCC's published summary files by
  geography are acquired (conditional on downloading them without an
  account); location-level availability and the Fabric are
  `not-implemented`.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| FCC ULS (`telecom-fcc-uls-licences`) | `unverified-live` | - | none; API and file layouts _verify_ |
| FCC BDC summary (`telecom-fcc-bdc-summary`) | `unverified-live` | - | none; conditional on account-free download (_verify_) |
| FCC BDC public data API | `gated-not-granted` | - | no account or token |
| FCC BDC location data and Fabric | `not-implemented` | - | location-level data out of scope; Fabric licence (_verify_) |
| Bundesnetzagentur | `not-implemented` | - | no stable machine access known (_verify_) |
| ITU DataHub | `not-implemented` | - | terms understood to restrict redistribution (_verify_) |

Fixtures, when written, are authored, not captured: fictional call signs and
FRNs, fictional companies, fictional FIPS-like geography codes marked as
fixtures, as-of dates in 2094-2097 with past-dated releases, so nothing can be
mistaken for a published record.

## Decision and recommendation

Two FCC sources survive as `unverified-live`: ULS licences of company
licensees in declared market-based services, and BDC published summaries,
conditional on account-free download. The BDC API is `gated-not-granted`;
BDC location data and the Fabric, the Bundesnetzagentur and ITU DataHub are
`not-implemented`.

Recommendation: open the TC per-track tracker (TC02-TC12, TC13 "Validate live
coverage"). First coverage is United States only; the guide and readiness
must say so, and European and global coverage stay explicit gaps within the
subdomain until an amendment adds a Bundesnetzagentur file or the SDG route
for ITU indicators.
