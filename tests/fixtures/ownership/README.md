# Corporate Ownership provider fixtures

**Authored fixtures, not live captures.** Every file here was written by hand to
mirror the response *shapes* that the corporate-ownership adapters target
(`src/ingestion/ownership_providers.py`, plus the existing GLEIF connector in
`src/ingestion/lei_sources.py`). All companies, officers, people, filings and
identifiers are **fictional**: the LEIs carry valid ISO 17442 check digits but
are not issued, the company numbers, CIKs, accession numbers and statement IDs
are invented, and names such as "Exampla" and "Northwind" describe no real
organisation. They exist to exercise acquisition, projection, reconciliation,
graph, timeline and dossier code paths offline and deterministically.

Each file is a source-pack fixture (`native_pages`, replayed by URL path
through `fixture_transport`) pinned by SHA-256 and expected-output hash in
`config/source_packs/corporate-ownership.json`. Editing a file requires
re-pinning both hashes; the offline conformance test fails otherwise.

Live coverage is validated separately by `scripts/ownership_live_check.py`,
whose output (`docs/development/ownership-evidence/`) records the provider,
time and failure codes apart from this offline evidence.

| File | Provider shape | Scenarios |
| --- | --- | --- |
| `gleif-level2.json` | GLEIF API v1 `lei-records`, `direct-/ultimate-parent-relationship`, `direct-/ultimate-parent-reporting-exception` (JSON:API) | direct and ultimate consolidation parents with relationship periods; `NO_KNOWN_PERSON` exceptions; a retired LEI naming a successor; registration-authority pointer (`RA000585` + registered-as number); unlisted parts replay as HTTP 404 = none reported |
| `companies-house.json` | Companies House public data API: company profile, officers, persons with significant control, PSC statements, exemptions, filing history | previous company name; officer appointment/resignation and an officer with no appointment date; corporate PSCs with 75–100 % bands that cease and are replaced (UK and non-UK registered); "no PSC" statement; regulated-market exemption; filing history across two `start_index` pages; requests without an `Authorization` header replay as HTTP 401 |
| `sec-edgar.json` | data.sec.gov submissions and companyfacts JSON; Archives primary documents | recent filings incl. `SCHEDULE 13G`, `SC 13G`, 20-F, 6-K; former name; shares-outstanding fact citing its filing; an XML Schedule 13G cover page with two reporting persons (8.2 %); an HTML 13G that stays an unparsed reference; requests without a `User-Agent` replay as HTTP 401. The 13G XML element names follow the EDGAR Schedule 13D/13G XML technical specification as read for the adapter and are unverified against live filings |
| `open-ownership-bods.json` | Open Ownership BODS 0.2 statements (JSON array) | entity, person and ownership-or-control statements with annotations; a person statement (kept owner-scoped); an unspecified interested party; a same-name decoy entity without identifiers; one statement from a source this pack does not implement (Denmark CVR, counted not projected); one BODS 0.4 record (unsupported version, counted) |
