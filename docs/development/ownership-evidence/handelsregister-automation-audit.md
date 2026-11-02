# Handelsregister and Unternehmensregister: automated retrieval audit (FA20)

Tracking: [#2859](https://github.com/Ikey168/Noesis/issues/2859) FA20 (tracker
[#2839](https://github.com/Ikey168/Noesis/issues/2839), free alternatives for
paid and limited connectors) · guide
[connector API keys](../../guides/connector-api-keys.md) · recorded 2026-11-01.

Question: can Noesis retrieve the free German register information (register
data and documents of the Handelsregister, Genossenschafts-, Partnerschafts- and
Vereinsregister, and the Unternehmensregister's publications) automatically,
through a documented interface that the operators permit?

**Outcome: not automatable through a permitted interface as of 2026-11-01.** No
documented machine interface or bulk export for third parties is known for
either portal, and the portal terms treat automated mass retrieval as misuse.
Nothing is scraped. The existing decisions stay: `handelsregister`,
`unternehmensregister` and `bris` remain `not-implemented` in
`src/ingestion/ownership_providers.py`, and an officially obtained extract is
recorded by hand with `OwnershipStore.record_register_document`
(`src/kb/ownership_store.py`). The routes to re-check are listed at the end.

**This audit was written without network access.** `www.handelsregister.de`,
`www.unternehmensregister.de`, `e-justice.europa.eu`, `offeneregister.de` and
`api.opencorporates.com` are blocked by this runtime's egress proxy (verified
2026-11-01). Terms, limits and fees come from the operators' documentation and
legal texts as the author knows them. Every item marked _verify_ must be checked
against the live terms before this decision is relied on or revisited.

## Current state in Noesis

| Place | What it records |
| --- | --- |
| `src/ingestion/ownership_providers.py`, `PROVIDER_CONTRACTS["handelsregister"]` | `not-implemented`; web portal of the Länder, extracts (AD/CD/HD) retrieved interactively; "portal terms restrict automated and bulk retrieval; no documented machine API"; free of charge since 2022, interactive only; a user-supplied official extract is recorded with `record_register_document` |
| same, `["unternehmensregister"]` | `not-implemented`; web portal of Bundesanzeiger Verlag; account for some documents; no documented public API; fees for some documents |
| same, `["bris"]` | `not-implemented`; BRIS via the e-Justice portal search; no public machine API for third parties |
| `src/ingestion/lei_sources.py`, `PROVIDER_CONTRACTS["official-registers"]` | `not-implemented`; the LEI record's registration authority and registered-as number are kept as the official pointer to the register entry |
| `src/ingestion/bafin_sources.py`, `["unternehmensregister"]` | `not-implemented` for issuer publications; link-only; officially obtained documents recorded with `record_ownership_register_document` |
| [`docs/guides/connector-api-keys.md`](../../guides/connector-api-keys.md) | Unternehmensregister row: "not implemented"; register information free without registration since August 2022, filed annual accounts €1 plus VAT |

The live check of 2026-09-27 ([README.md](README.md)) did not include these
portals; they have no adapter to run.

## Findings per route

### 1. Gemeinsames Registerportal der Länder (handelsregister.de)

- **Free access.** Since 1 August 2022, inspection of the register and of the
  documents filed with it is free of charge and needs no registration
  (DiRUG, Gesetz zur Umsetzung der Digitalisierungsrichtlinie, amending § 9 HGB
  and the fee rules; _verify_ citation BGBl. I 2021 p. 3338 and the date). This
  covers the current, chronological and historical printouts (AD, CD, HD), the
  structured register content (SI, an XJustiz XML file per company, _verify_)
  and the document folder.
- **Machine interface.** None documented for third parties (_verify_). The
  portal is an interactive web application (session-bound forms); there is no
  published API, no API key scheme and no bulk export.
- **Terms.** The portal's terms of use treat more than 60 retrievals per hour
  as misuse (_verify_ the exact wording and figure in the current
  Nutzungsbedingungen); automated querying of the search forms is not offered
  as a use of the portal (_verify_). Community tools that drive the search
  forms programmatically exist; they are scraping the interactive portal, not
  using a permitted interface, and Noesis does not use or copy them.
- **Conclusion.** Not automatable through a permitted interface. Throttling a
  scraper under the hourly limit would still be automated use of an interface
  offered for interactive inspection; the limit defines misuse, it does not
  grant a machine-access right.

### 2. Unternehmensregister (unternehmensregister.de, Bundesanzeiger Verlag)

- **Content.** Central access point for company publications: register data
  mirrored from the register courts, filed annual financial statements
  (Jahresabschlüsse, filed directly with the Unternehmensregister since 2022,
  _verify_), capital-market notices and other publications.
- **Cost.** Register information is free without registration since August 2022.
  Filed annual accounts cost **€1 plus VAT per document** (as recorded in
  `docs/guides/connector-api-keys.md` and #2859; _verify_ the current fee
  schedule and whether it applies per document or per retrieval). Some
  documents need a user account.
- **Machine interface.** None documented for retrieval (_verify_). The
  Bundesanzeiger Verlag offers electronic submission interfaces for filers;
  these are for filing, not for retrieval, and do not change this finding
  (_verify_).
- **Terms.** Automated retrieval is not offered; the terms restrict systematic
  extraction (_verify_ wording).
- **Conclusion.** Not automatable through a permitted interface. Paid documents
  would also need a payment flow per document, which a free-alternative
  connector cannot provide.

### 3. EU interconnection: BRIS and the e-Justice portal

- **Content.** The Business Registers Interconnection System connects the
  member states' registers; the European e-Justice portal offers a "find a
  company" search. Directive (EU) 2017/1132 as amended by Directive (EU)
  2019/1151 makes a minimum set of company information free through BRIS;
  Directive (EU) 2025/25 extends the information available and introduces an
  EU Company Certificate, with transposition deadlines after this audit's date
  (_verify_ dates).
- **Machine interface.** BRIS is a closed network between registers and the
  portal; no public API for third parties is documented (_verify_).
- **Conclusion.** Not a permitted machine route today. The e-Justice search is
  interactive and is not scraped.

### 4. High-value datasets: company and company ownership

- **Obligation.** Commission Implementing Regulation (EU) 2023/138 lists
  "companies and company ownership" as a high-value dataset category: basic
  company information (name, status, registration date, registered address,
  legal form, register number, activity where held) must be available free of
  charge, through APIs and as bulk download, under CC BY 4.0 or an equivalent
  or more permissive licence, from 9 June 2024 (_verify_ the annex and date).
- **German implementation.** The author does not know of a documented German
  API or bulk export implementing this category for the Handelsregister
  (_verify_). The planned Basisregister für Unternehmen (Unternehmensbasisdatenregister,
  operated by Destatis with a bundeseinheitliche Wirtschaftsnummer) may become
  a related source (_verify_ status and access).
- **Conclusion.** This is the **permitted route to watch**: if Germany publishes
  a documented HVD API or bulk file for company data, it is a candidate
  adapter (basic company data, not extracts or filed documents), to be audited
  as an amendment to this file.

### 5. Third-party bulk copies

| Route | What it is | Terms | Decision |
| --- | --- | --- | --- |
| OffeneRegister.de (Open Knowledge Foundation Deutschland with OpenCorporates) | one bulk dump of German register data published in 2019 (about 5 million companies, officers, SQLite/JSONL, _verify_) | open licence stated by the publishers (_verify_ which; the data came from OpenCorporates' harvest, whose own data licence is ODbL-style share-alike, _verify_) | **not used**: a stale 2019 snapshot from a third-party harvest of the portal, not an official extract; it would be a republisher of the register courts and never a current register state |
| OpenCorporates | aggregator that includes German companies harvested from the register portal | API key; free share-alike key only for public-benefit users, otherwise paid (see the OpenCorporates row in the guide) | **already reused** through the existing regional provider as enrichment, never as an official register; changes nothing here |
| Commercial register-data resellers | paid services built on the portal | commercial licences | out of scope (paid) |

## Decision

- **Not automatable through a permitted interface** (2026-11-01, offline).
  Reasons: (1) neither handelsregister.de nor unternehmensregister.de documents
  a machine interface or bulk export for third parties; (2) the portal terms set
  a retrieval limit (60 per hour, _verify_) and treat automated mass retrieval as
  misuse; (3) filed annual accounts cost €1 plus VAT per document, which a free
  connector cannot pay; (4) BRIS has no third-party API; (5) the only bulk
  copies are third-party harvests (a stale 2019 dump and OpenCorporates), not
  official extracts.
- `PROVIDER_CONTRACTS` for `handelsregister`, `unternehmensregister` and `bris`
  stay `not-implemented`; no code changes.
- **Permitted path that exists today:** a person retrieves an extract
  interactively (free), or a filed document (€1 plus VAT), and Noesis records it
  with `OwnershipStore.record_register_document` with its document reference.
  The SI file (XJustiz XML) is the structured form of such an extract; parsing a
  user-supplied SI file is a possible follow-up, not part of this audit.
- **Re-check triggers:** a documented HVD company-data API or bulk export for
  Germany; a documented interface on handelsregister.de or
  unternehmensregister.de; the transposition of Directive (EU) 2025/25 opening a
  third-party route. Each would be audited as an amendment to this file before
  any code.
