# Political lobbying: source-contract audit and provider coverage (T01)

Tracking: #1911 · delivery issue #1925 · recorded 2026-09-27.

This audit fixes, per source, what the Political pack's optional `lobbying`
feature may acquire, how, and on what terms. It was written without network
access: endpoints, formats, identifiers and terms are recorded from the
providers' published documentation as known to the author. **Every item
marked _verify_ must be checked against the live page before the first dated
live run (T12, #2024), and no source is `live` until that run exists.** The
machine-readable copy of these decisions is `PROVIDER_CONTRACTS` in
`src/ingestion/lobbying_sources.py`; the MCP tool `lobbying_source_contracts`
returns it.

Non-goals apply to every source: declarations are what a registrant or an
official filed. No influence or corruption claim, no undeclared-lobbying
inference, and no aggregation of a declared spend range into a point estimate,
total or average. Registers are never merged; conflicting declarations are kept
side by side.

## Access decisions

| Source | Decision | Reason |
| --- | --- | --- |
| EU Transparency Register (open-data export) | `unverified-live` | parser and fixtures follow the XML export shape; element names and the export URL are _verify_; a redirect to another host (for example data.europa.eu) is refused by the runtime transport |
| German Lobbyregister (JSON export) | `unverified-live` | parser follows the JSON export of register entries; field names, the endpoint and whether the announced API needs a key are _verify_ |
| European Parliament MEP meeting declarations | `unverified-live` | parser follows a CSV export of the meetings search; column names and the export URL are _verify_ |
| European Commission meetings (Commissioners, cabinets, Directors-General) | `unverified-live` | parser follows a documented JSON shape; a machine-readable Commission export must be verified, otherwise the source stays unavailable (HTML pages are never scraped) |
| UK Register of Consultant Lobbyists | `unverified-live` | parser follows the register CSV download; column names and the URL are _verify_ |
| Bundestag party financing (Rechenschaftsberichte, Großspenden) | `not-implemented` | PDF reports and an HTML donations list only; no documented machine-readable export; party financing is not a lobbying declaration |
| Integrity Watch EU (Transparency International EU) | `not-implemented` | an aggregator whose reuse terms are not verified; not used even as a fixture-only cross-check |

**Unavailable-access fallback.** When an export cannot be fetched (HTTP
error, redirect refused, budget exceeded, schema drift), the run records the
failure class and no export is written. Earlier exports stay authoritative
for their dates; as-of answers after the last acquired export report the last
revision in force, and a deregistration is only ever derived from a *newer*
full export. An export is all-or-nothing: a partial file is never stored,
because missing entries would read as deregistrations.

**Live evidence and notifications.** Exports acquired through the runtime's
HTTPS transport are recorded as `live` evidence, fixture replays as `fixture`
evidence. Monitors withhold live revisions of a source whose decision is still
`unverified-live` (no change notification) until a dated live run moves it to
`verified-live`.

## Record kinds per source

| Source | Registrant | Client | Declared interest | Spend range | Meeting | Revision history |
| --- | --- | --- | --- | --- | --- | --- |
| EU Transparency Register | yes | yes (consultancies) | fields of interest, EU legislative files | cost or revenue range per closed financial year; per-client revenue ranges; declared EU grants | no (Commission meetings are separate) | current entry only; revisions are the exports acquired here |
| Lobbyregister | yes | clients and principals | fields of interest, regulatory projects with printed-paper numbers | annual financial-expenditure range | no | entries carry a version number and validity date; earlier versions shown on the entry page (_verify_ machine-readable access) |
| EP meeting declarations | no | no | subject, procedure reference when declared | no | yes | none; a changed declaration is observed as a changed row |
| Commission meetings | no | no | subject, legislative files when declared | no | yes | none |
| UK consultant lobbyists | yes | yes, per quarterly return | no | no | no | each quarterly return is its own revision |

## Per-source contracts

### EU Transparency Register

- **Access:** open-data XML export of all registrants, linked from
  `transparency-register.europa.eu` and data.europa.eu (_verify_ the current
  URL; the configured endpoint is the historic `getLobbyistsXml` export).
- **Terms:** Commission reuse policy (Decision 2011/833/EU), attribution
  (_verify_ the register's own notice).
- **Auth / limits / pagination:** none / undocumented, one download per run /
  none (one full file).
- **Cadence:** regenerated daily (_verify_).
- **Identifiers:** identification number `NNNNNNNNNNNN-NN`; client entries may
  carry the client's own number.
- **Fields mapped as filed:** name, legal status, section and category, head
  office, website, fields of interest, EU legislative proposals (structured
  `procedureReference`/`celex`/`eli` attributes, or exact procedure-reference
  and CELEX patterns inside that field), closed-year costs or revenue range
  with currency and financial year, per-client revenue ranges, declared EU
  grants (programme, source and amount as filed; cross-referenced to
  `funding.opportunities` programme records only as candidates).
- **Revisions and deregistrations:** each export adds a revision only for
  changed entries (content digest); an entry absent from a newer full export is
  a `deregistered` lifecycle revision; one that reappears is `reregistered`.

### German Lobbyregister

- **Access:** JSON export of register entries (`sucheJson`) (_verify_; an API
  with keys is announced - _verify_ whether a key becomes necessary).
- **Terms:** Bundestag open-data terms, attribution (_verify_).
- **Identifiers:** register number `R000000`, entry id and version; an entry
  may state the EU TR number and an LEI.
- **Fields mapped as filed:** legal form, fields of interest, clients and
  principals, regulatory projects (`RV…`) with Bundestag printed-paper numbers
  (Drucksache, stored as explicit fields for dossier links), the annual
  financial-expenditure range with fiscal year, and statements/position papers.
- **Documents:** position papers are stored as link-only references with the
  revision they were attached to; their text is kept only when the source's
  `documents` retention is set to `text` after the terms are verified.
- **Deregistrations:** `activeLobbyist=false` or absence from a newer full export.

### European Parliament meeting declarations

- **Access:** MEP pages and the meetings search with a CSV export (_verify_).
- **Terms:** European Parliament legal notice, reuse with attribution (_verify_).
- **Identifiers:** MEP identifier; the procedure reference where the MEP
  declares one (rapporteurs, shadow rapporteurs, committee chairs); an EU TR
  number written beside an organisation. The organisation string is always
  kept, even when a number is present.
- **Meeting identity:** no native id is published; the id is a digest of MEP,
  date, title and attendees, so a corrected declaration is a new row.

### European Commission meetings

- **Access:** per-Commissioner meeting lists and the Transparency Register
  meeting pages (_verify_ a machine-readable export).
- **Terms:** Commission reuse policy (_verify_).
- **Identifiers:** official name and role, each organisation's TR number.

### UK Register of Consultant Lobbyists

- **Access:** register CSV download (_verify_ URL and columns).
- **Terms:** Open Government Licence v3.0 (_verify_).
- **Identifiers:** ORCL registrant reference; Companies House number where
  declared (compared only within the GB register).
- **Revisions:** each quarterly return (`YYYY-Qn`) is its own register
  revision with the clients declared in it.

### Bundestag party financing - `not-implemented`

Rechenschaftsberichte are Bundestag printed papers (PDF) and large donations
are listed on an HTML page. There is no verified machine-readable export, and
party financing is not a lobbying declaration. It is not scraped. It may be
added as a separate source once a documented export exists.

### Integrity Watch EU - `not-implemented`

Integrity Watch re-publishes register and meeting data. Its reuse terms are
not verified, so it is not used, not even as a fixture-only cross-check. If
its terms are verified it may only cross-check primary-register records and
keeps its own source identity; it never replaces a primary register.

## Bounded coverage

`config/source_packs/political.json` (1.1.0) adds `eu-transparency-register`,
`de-lobbyregister`, `ep-mep-meetings`, `ec-meetings` and
`uk-consultant-lobbyists` on the `lobbying-register` connector with byte,
result and page budgets, a daily or quarterly cadence, `health.required=false`
and pinned, authored fixtures (`tests/fixtures/source_packs/political-lobbying-*.json`,
fictional parties). A source may name a bounded `selection.native_ids` list;
absence-based deregistration then applies only to the selected entries.
