# Legal sanctions: source-contract audit and provider coverage (S01)

Tracking: #1907 · delivery issue #1910 · recorded 2026-09-27.

This audit fixes, per source, what the Legal pack's `sanctions` feature may
acquire, how, and on what terms. It was written without network access:
endpoints, formats and terms are recorded from the providers' published
documentation as known to the author. **Every item marked _verify_ must be
checked against the live page before the first dated live run (S12, #1998),
and no source is `live` until that run exists.** The machine-readable copy of
these decisions is `PROVIDER_CONTRACTS` in `src/ingestion/sanctions_sources.py`
and `COMEXT_SELECTION` in `src/kb/sanctions_trade.py`; the MCP tool
`sanctions_source_contracts` returns both.

Non-goals apply to every source: no screening verdict, no sanctions or AML
compliance determination, no legal advice, and no inference that a similar
name is the same party. Lists are never merged.

## Access decisions

| Source | Decision | Reason |
| --- | --- | --- |
| EU consolidated financial sanctions list (FSF) | `unverified-live` | parser and fixtures follow the FSF XML 1.1 shape; no dated live run |
| EU Sanctions Map | `not-implemented` | no documented machine-readable interface verified; the site is never scraped. Programme and act context comes from the FSF file and CELLAR |
| UN Security Council Consolidated List | `unverified-live` | parser follows the published XML; confirm the download is served without a cross-host redirect |
| OFAC Sanctions List Service | `unverified-live` | parser follows the legacy SDN.XML export. SLS exports may be delivered through a cross-host redirect to cloud storage, which the runtime transport refuses (`network_policy`); a same-host export URL must be verified |
| UK Sanctions List (FCDO) | `unverified-live` | parser follows the UK Sanctions List XML; _verify_ the current download URL and schema |
| Regulation (EU) 2021/821 and Annex I amendments (CELLAR) | `unverified-live` | existing `cellar` connector; consolidated-version CELEX dates in the pinned selection are _verify_ placeholders |
| Eurostat Comext | `unverified-live` | existing Eurostat dataset connector on the Comext dissemination path; dataset code _verify_ |

**Unavailable-access fallback.** When a list cannot be fetched (HTTP error,
redirect refused, budget exceeded, schema drift), the run records the failure
class and no snapshot is written. Earlier snapshots stay authoritative for
their dates, and as-of answers after the last acquired snapshot say that no
later snapshot was acquired. A partial file is never stored, because missing
entries would read as delistings.

## Per-source contracts

### EU consolidated financial sanctions list (FSF)

- **Access:** full-list file download linked from data.europa.eu, XML 1.1
  (`https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=…`).
  The token is a public value printed in the published URL, not a credential
  (_verify_). CSV and PDF renderings exist and are not used.
- **Terms:** Commission reuse policy (Decision 2011/833/EU) (_verify_ the FSF notice).
- **Auth / limits / pagination:** none / undocumented (one download per run) / none.
- **Cadence:** republished whenever a Council or Commission act changes a
  listing, often several times a month.
- **Snapshots or deltas:** full snapshots only.
- **Revisions and delistings:** not expressed in the file. An amended entry is
  a changed entry in the next file; a delisted entry disappears. Revisions
  and delistings are derived by comparing successive snapshots and cite both.
- **Identifier:** EU reference number (`euReferenceNumber`); the FSF
  `logicalId` and a UN reference (`unitedNationId`) are kept as cross-references.
- **Aliases:** `nameAlias` (whole name, language, `strong` flag),
  `identification` (passport, national ID, registration number, IMO …),
  `birthdate`, `citizenship`, `address`, `remark`.
- **Legal act:** per-entry `regulation` element with `numberTitle`,
  `publicationUrl` (an EUR-Lex link carrying the CELEX number), `programme`,
  `regulationType` (e.g. `amendment`) and entry-into-force date. The CELEX is
  resolved to a Legal work by exact identifier (below).
- **Retained evidence:** file digest and `generationDate` per snapshot; the
  runtime keeps one document per entry.

### EU Sanctions Map

Interactive web application. No documented export or API was found, so it is
`not-implemented` and is never scraped.

### UN Security Council Consolidated List

- **Access:** `https://scsanctions.un.org/resources/xml/en/consolidated.xml`
  (_verify_), HTML/PDF renderings also exist.
- **Terms:** United Nations website terms of use (_verify_).
- **Auth / limits / pagination:** none / undocumented / none.
- **Cadence:** after each committee listing, amendment or removal.
- **Snapshots or deltas:** full snapshots (`dateGenerated`); press releases
  announce changes and are not parsed.
- **Revisions and delistings:** `VERSIONNUM` and `LAST_DAY_UPDATED` per entry
  are kept as stated; the revision chain is derived by snapshot comparison.
- **Identifier:** permanent reference number (`REFERENCE_NUMBER`, e.g.
  `QDi.…`); `DATAID` kept as a cross-reference.
- **Aliases:** `*_ALIAS` with `QUALITY` (Good/Low), `NAME_ORIGINAL_SCRIPT`,
  `*_DOCUMENT` (passport, national ID), dates of birth, addresses, nationality.
- **Legal act:** the regime (`UN_LIST_TYPE`) is stored as the programme. The
  Security Council resolutions are not CELLAR works; no legal-basis record is
  invented.

### OFAC Sanctions List Service

- **Access:** SLS exports; the legacy `SDN.XML`
  (`https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML`, _verify_).
  `SDN_ADVANCED.XML` (with designation dates) and delta files exist and are
  not used in this bounded selection.
- **Terms:** U.S. government work, public domain (_verify_ SLS terms).
- **Auth / limits / pagination:** none / undocumented / none.
- **Cadence:** with each OFAC action (Recent Actions).
- **Snapshots or deltas:** both exist; full snapshots are used.
- **Revisions and delistings:** derived by snapshot comparison; the legacy XML
  carries no designation date, so `listed_on` stays unknown.
- **Identifier:** OFAC UID (`uid`); programme tags (`programList`).
- **Aliases:** `akaList` with type (a.k.a., f.k.a.) and category
  (strong/weak), `idList` (passport, IMO, registration …), dates of birth,
  addresses, `vesselInfo` (call sign kept as an identifier).
- **Legal act:** programme tags only; executive orders and statutes are not
  CELLAR works.
- **Transport:** if the export answers with a redirect to another host, the
  runtime's same-host redirect policy refuses it (`network_policy`). The
  source stays `unverified-live` until a same-host URL is confirmed; the
  policy is not relaxed.

### UK Sanctions List (FCDO)

- **Access:** XML download on `sanctionslist.fcdo.gov.uk` (_verify_ URL and
  schema; CSV and ODS also published). The OFSI consolidated list is not used.
- **Terms:** Open Government Licence v3.0 (_verify_).
- **Auth / limits / pagination:** none / undocumented / none.
- **Cadence:** with each designation, variation or revocation.
- **Snapshots or deltas:** full snapshots.
- **Revisions and delistings:** `LastUpdated` and `DateDesignated` kept as
  stated; chain derived by snapshot comparison.
- **Identifier:** unique ID (`UniqueID`); OFSI group ID and UN reference
  number are cross-references.
- **Aliases:** `Names` with `NameType` and `AliasStrength`, `NonLatinNames`
  with script and language, passport and national identifier details, ship IMO
  numbers, business registration numbers, addresses.
- **Legal act:** regime name. UK regulations made under SAMLA 2018 are not
  CELLAR works and stay citation strings.

### Regulation (EU) 2021/821 and its Annex I amendments (CELLAR)

- **Access:** the existing `cellar` connector in `src/ingestion/legal_sources.py`
  (public SPARQL endpoint `publications.europa.eu/webapi/rdf/sparql`,
  `cellar_query` by explicit CELEX). The connector now optionally fetches the
  XHTML item of selected manifestations from the same host and splits it into
  located passages (`xhtml-paragraphs`, or `eu-control-list-annex`, one
  passage per control code).
- **Which queries resolve what:** `cellar_query` with CELEX `32021R0821`
  resolves the base act; annex-amending Commission delegated regulations
  (e.g. `32024R2547`, _verify_ the series) resolve as their own works linked by
  the `amends` triple; consolidated versions (`02021R0821-YYYYMMDD`) resolve as
  editions of one consolidated work, and each carries the full Annex I text.
  Sanctions acts cited by the EU list (e.g. `32014R0269`) resolve the same way
  and are what legal-basis records link to.
- **Editions and as-of:** each consolidated version is a `legal_versions`
  row with a `consolidation` fact from its CELEX date; `select_legal_version_as_of`
  selects the edition applying on a date, a later acquired edition supersedes
  an earlier one from its own date, and equal dates stay ambiguous. Current
  force is not inferred; the answer states that only acquired editions are
  compared.
- **Terms:** EU publications reuse (Decision 2011/833/EU).

### Eurostat Comext

- **Access:** the existing Eurostat dataset connector
  (`src/ingestion/connectors/dataset/eurostat.py`) now accepts `api: comext`,
  which uses the Comext dissemination path
  (`https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/<dataset>`)
  and the `reporter` dimension (_verify_ path and dataset code `DS-045409`).
  The SDMX connector (`sdmx.py`) supports only the ECB, ESTAT and BBK
  dissemination bases, and the Comext SDMX base is not among them, so no
  Comext dataset is addressed through it; no new connector type is added.
- **Terms:** Eurostat reuse policy with attribution.
- **Vintages:** one per provider `updated` timestamp; re-acquiring an
  unchanged cube adds no vintage and keeps the first retrieval time.
- **Correlation:** control codes and CN8/HS6 codes are different
  classifications. Their mapping is a sourced, revisioned correlation table
  marked as a lookup aid (the Commission's published correlation table,
  _verify_), never a statement that goods under a product code are controlled.

## Fixtures

All fixtures under `tests/fixtures/sanctions/` and the `legal-sanctions-*` /
`legal-cellar-{sanctions-acts,dual-use}-eng` source-pack fixtures are
**authored** in the documented shapes, with fictional parties, cellar URIs and
annex wording. They are pinned by SHA-256 in `config/source_packs/legal.json`
(`legal-research` 1.1.0) and are offline evidence only.
