# Extractives and natural resources: source audit, minimisation decision and bounded coverage (EX01)

Tracking: #2653 · delivery issue #2657 · recorded 2026-09-30.

This audit sets out, per source, what the Economics bundle's `economics.extractives`
provider may acquire, how, and on what terms. **It was written without network
access to the providers: eiti.org, usgs.gov, sciencebase.gov and bgs.ac.uk were
unreachable from the authoring environment (egress blocked), so the terms,
endpoints, field names and column headers below come from the providers'
published documentation as the author knows it and from the tracker's
references, and were not re-verified live.** Every item marked _verify_ must be
checked against the live documentation, the live terms and a real response
before the first dated live run (EX13, #2717). No provider is `live` until that
run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`MINIMISATION`, `LIVE_VERIFICATION` and `BOUNDED_COVERAGE` in
`src/ingestion/extractives_sources.py`; the MCP tool
`extractives_source_contracts` returns them, and every source entry of the
source pack `config/source_packs/economic-extractives.json`
(`economic-extractives` 1.0.0) carries its `extractives.live_verification`
status (`unverified-live`).

**Source-pack placement.** The issue asked for entries in
`config/source_packs/economic.json`. They live in a separate economics-domain
source pack instead, following the logistics feature's precedent: adding them
to `economic-statistics-and-filings` would bump that pack's version and the
Economics bundle's pin, which the other Economics features depend on. The
Economics bundle pins both packs; `economic.json` is unchanged.

Non-goals for every source: payments, production and reserves are stored as
each publisher released them. No reconciliation of payment discrepancies beyond
what the EITI report states, no own reserve or production estimate (a withheld
value stays withheld), no corruption or governance risk scoring, no price
forecast, no currency conversion and no sum across reports, no blending of
USGS and BGS series, and no project ownership inferred from names or places.

## Access decisions

| Source | Delivers | Feature | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- | --- |
| EITI summary data (eiti.org) | per implementing country, fiscal period and report version: report, government agencies, GFS-classified revenue streams, companies, projects and licences as reported, company payments with government- and company-reported figures and the report's reconciliation discrepancies | `extractives-eiti` | `unverified-live` | Open data without authentication. The API path (`/api/v2.0/summary_data/...`), the JSON field names of the summary-data template and the report-version metadata are _verify_ |
| USGS Mineral Commodity Summaries (sciencebase.gov) | world mine production and reserves by country per commodity, one data release per year | `extractives-usgs` | `unverified-live` | US federal data release without authentication; the ScienceBase item ids, file names and per-year column headers are _verify_ |
| BGS World Mineral Statistics (www2.bgs.ac.uk) | production, imports and exports by country and commodity | `extractives-bgs` | `unverified-live` | Free download that requires accepting the BGS terms in the query; the query parameters, CSV layout and licence wording are _verify_ |

No source was found whose terms forbid the intended use, so no provider is
recorded as "not implemented". If the live check finds BGS redistribution
restricted to non-commercial use, the BGS feature stays off by default (it
already is) and the restriction is added to its licence entry before any live
run.

## Per-source contract

| Source | Endpoint, authentication and key handling | Identifiers | Licence and redistribution | Rate limits | Updates, corrections and removals |
| --- | --- | --- | --- | --- | --- |
| EITI | `GET https://eiti.org/api/v2.0/summary_data/{id}` (_verify_), JSON rendering of the summary data template v2; no authentication, no key is handled | report = ISO 3166-1 alpha-2 country + fiscal period start and end; revenue streams by GFS code as published (`1112E1`, `1415E1`); companies by the identification number and register the report publishes, else the name as reported; projects and licences as reported | EITI open data: free reuse with attribution to EITI and the national report (_verify_ the licence statement) | not documented; one request per declared report version, bounded by `max_pages` | a revised report is a **new report version** declared by the operator (version and publication date); each changed record adds a revision; a record the new version no longer states becomes a `removed` revision; an older version arriving after a newer one is refused (`stale_version`) |
| USGS MCS | `GET https://www.sciencebase.gov/catalog/file/get/{item}?name={file}` (_verify_), CSV per commodity table; no authentication | commodity = MCS commodity name with declared unit and definition; countries as published names, ISO 3166-1 alpha-2 and M49 codes **declared by the operator** per name (basis `operator-declared`), world total kept as a published aggregate row | US federal government works, public domain; cite USGS (_verify_) | not documented; one download per declared table | each annual release (late January) is a **vintage**; the latest year's column is estimated (flag stored), `e`/`r` markers stored as published, `W` (withheld to avoid disclosing company proprietary data) stays withheld, never filled; a year estimated in one release and final in the next shows both vintages |
| BGS WMS | `GET https://www2.bgs.ac.uk/mineralsuk/statistics/wms.cfc?method=listResults&...&agreeToTsAndCs=agreed` (_verify_), CSV export; no account | commodity and sub-commodity names as published; statistic type (Production, Imports, Exports) mapped to `production`, `imports`, `exports`; countries as published names with operator-declared codes | acknowledgement "British Geological Survey (c) UKRI" required; redistribution terms (Open Government Licence or BGS non-commercial terms) _verify_ | not documented; one download per declared query | each BGS publication is a **vintage**; later publications revise earlier years; values `..` and similar markers stay `not_available` |

## Personal data and the minimisation decision

| Source | Personal data | Decision |
| --- | --- | --- |
| EITI | contact persons of national secretariats, multi-stakeholder groups and reporting entities (names, e-mail, telephone); beneficial owners (natural persons) where beneficial-ownership disclosure is attached; signatories; payers or licence holders the report marks as individuals (e.g. artisanal licence holders) | **Excluded:** contact persons, beneficial owners, signatories - never parsed into a record. **Redacted:** an individual payer or licence holder keeps its payments under a report-local key; its name is replaced by `[natural person - name withheld]` and its identifier dropped. **Stored:** legal-person company names and published company identifiers, government agency names, project names, licence numbers and published coordinates, amounts as reported. |
| USGS MCS | none | no decision needed |
| BGS WMS | none | no decision needed |

- **Retention:** a record is kept as long as the report revision it belongs to;
  redaction applies to every revision.
- **Who may query:** holders of `knowledge:extractives:read` in the namespace.
  Because no personal field is stored, no tool can return one.
- **Enforcement at write time:** `check_minimised` in
  `src/kb/extractives_records.py` refuses any record carrying a personal key
  (`contact`, `email`, `phone`, `beneficial_owners`, `signatory`, ...) or an
  individual whose name is not withheld (`personal_data_refused`); the parser
  never copies those fields. Individual payers are never offered to identity
  matching.

## Bounded coverage (selected)

| Provider | Scope | Places | Periods | Caps |
| --- | --- | --- | --- | --- |
| EITI | summary data of two implementing countries | NL (Netherlands EITI), DE (D-EITI) | the two most recent reported fiscal years, every published version | `max_results` records per report version (a version is never truncated), 2 documents per run |
| USGS MCS | copper and lithium world production and reserves tables | every country row of the declared tables and the world total | the two most recent annual releases | `max_results` series per table |
| BGS WMS | copper production and ores imports/exports; crude petroleum production | CL, PE, DE, NL | five reference years per publication, the two most recent publications | `max_results` series per query |

Justification: copper is published by all three sources (EITI payments of
mining companies, USGS and BGS production), so the side-by-side answer and the
trade link (HS heading 2603) are exercised on one commodity; crude petroleum
exercises the Energy balance link by its SIEC code; the Netherlands and
Germany are EITI implementing countries whose reports can name companies of the
Corporate Ownership fixtures' Exampla group. The offline fixtures use these
selections with fictional companies and values; every declared release date
lies in the past, so a real retrieval always follows it.

## Gap against existing components

- The Economics series storage (`register_series`, `dataset_*`,
  `economic_vintages`) keys a series by one geography string and has no place for
  withheld/estimated/revised markers, notes or the operator-declared country code
  basis. Those live in `extractives_values` and `extractives_series` beside it;
  every number is registered once in the shared storage with its release clock.
- EITI records are not numeric series: they are revisioned records per report
  version in `extractives_records`, following the append-only pattern of
  `src/kb/entity_history.py`.
- Company matching reuses the ownership reviewable identity state machine
  (`src/kb/ownership_identity.py`); `extractives:` joins its foreign-key prefixes
  so these links never regroup ownership entities.
- Commodity-to-HS mapping needs a published correspondence; none is bundled with
  the trade concordances (which map HS editions, not commodities), so an
  operator records the one the publisher states (BGS lists the HS headings it
  uses for trade statistics, _verify_) with its citation.
