# Economics demographics: source-contract audit and provider coverage (M01)

Tracking: #1914 · delivery issue #1924 · recorded 2026-09-27.

This audit sets out, per source, what the Economics pack's optional
`demographics` feature may acquire, how, and on what terms. It was written
without network access. Endpoints, layouts, identifiers and terms come from
the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live page and a
published response before the first dated live run (M12, #2023). No provider
is `live` until that run exists.** The machine-readable copy of these decisions
is `PROVIDER_CONTRACTS` in `src/ingestion/demographic_sources.py`. The MCP tool
`demographic_source_contracts` returns it.

These non-goals apply to every source:

- Values are what the publisher released, each with its definition revision,
  unit, geography level, flags and source revision.
- Series from different publishers, definitions or geography levels stay
  separate. Comparability between them is an explicit, reviewable note.
- Nothing projects a population or claims a cause of migration or a policy
  effect.
- No value is merged, averaged, netted, chained across a break, rebased or
  apportioned to another geography.
- PDF-only and portal-only publications are never scraped.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| Eurostat migration/asylum and population/demography databases | population stocks (1 January), immigration and emigration flows, asylum applications and first-instance decisions | `unverified-live` | Acquired as JSON-stat 2.0 through the existing Eurostat dataset connector. The dataset's `updated` time is the vintage, and each observation's status flag is kept (a new opt-in `retain_status` spec key). Dataset codes, filters and the current flag list are _verify_ |
| UNHCR Refugee Data Finder API | refugee, asylum-seeker, IDP, stateless and others-of-concern stocks | `unverified-live` | The parser follows `/population/v1/population/` (`items`, `coo_iso`, `coa_iso`, one field per population type, `maxPages`). The parameter names (`yearFrom`, `yearTo`, `coo`, `coa`, `cf_type`) and field names are _verify_. The response carries no release timestamp, so the release (Global Trends, Mid-Year Trends) is declared with its publication date, or `Last-Modified` is used. A response with neither is refused |
| IOM DTM API | IDPs present per admin area and round | `unverified-live` (credentialed) | The DTM API needs a subscription key (`Ocp-Apim-Subscription-Key`, secret `NOESIS_IOM_DTM_KEY`). The parser follows the documented `isSuccess`/`result` envelope with `operation`, `admin0Pcode`, `admin1Pcode`, `numPresentIdpInd`, `reportingDate` and `roundNumber`. The version path and field names are _verify_. HDX and portal downloads are not scraped |
| BAMF asylum figures | first-time and follow-up applications, decisions by outcome | `not-implemented` (automated) | Asylgeschäftsstatistik and "Aktuelle Zahlen" are PDF reports with no documented machine-readable export (_verify_), so they are never scraped. Figures enter as operator-recorded figure sheets (`import_demographic_figure_sheet`), each with report URL, page and table locator and the report's own wording. Machine-readable German asylum series come from Eurostat `migr_asy*`, which BAMF reports |
| Destatis GENESIS-Online, tables 12411 and 12711 | population by Land and nationality (31 December); migration between Germany and abroad | `unverified-live` (credentialed) | REST API 2020: `metadata/table` for the table's `Updated` stamp (the table version) and `data/tablefile?format=ffcsv` for values. Credentials (token, secret `NOESIS_DESTATIS_GENESIS_TOKEN`) go in request headers. Header names after the 2024 authentication change, table numbers (12411-0010, 12711-0005) and ffcsv columns are _verify_ |
| Statistik Berlin-Brandenburg | district population and foreign population | `unverified-live` | District CSV files in a declared column layout (Stichtag, Bezirk, Bezirksname, value columns). Every column must be declared, or the file is refused. File URLs and columns are _verify_. PDF Statistische Berichte are not scraped |

**Unavailable-access fallback.** A publication is not stored when any of these
happens: an HTTP error, a redirect to another host, a missing credential, an
exhausted budget, schema drift, an undeclared column, population type, value
code or unit, a multi-page UNHCR answer, or no stated release date. The run
records the failure class, and earlier releases stay authoritative. A release
is all-or-nothing, so a partial publication is never stored. A provider whose
stored release timestamp is unchanged but whose values changed is refused with
`vintage_conflict`. The stored vintage is kept.

**Live evidence and notifications.** Releases acquired through the runtime's
HTTPS transport are marked `live`, fixture replays `fixture`, and operator
sheets `operator`. Monitors withhold live releases from providers that are
still `unverified-live`, until a dated run verifies the provider.

## Definitions, geography levels and vintages per source

| Source | Definitions it publishes | Geography levels | Revision and vintage semantics | Lowest unit |
| --- | --- | --- | --- | --- |
| Eurostat | Stock (`migr_pop*`, `demo_pjan`: 1 January) versus flow (`migr_imm*`, `migr_emi*`: calendar year). Citizenship (`citizen`) versus country of birth (`c_birth`). Applications (`applicant`: first-time or subsequent) versus first-instance decisions (`decision`). Reference date versus reference period | Country codes. NUTS 1/2/3 with the NUTS version (NUTS 2021 current; _verify_ per dataset) | Dataset `updated` timestamp. Each update may revise earlier periods. Flags: `p` provisional, `e` estimated, `b` break in time series, `c` confidential, `u` low reliability, `d` definition differs (_verify_) | persons (`NR`, `PER`) |
| UNHCR | Population types as UNHCR defines them, each its own definition revision. Year-end stocks, never flows. Country of origin and country of asylum are separate dimensions | Country (ISO 3166-1 alpha-3) | Annual (June) and mid-year releases. No response timestamp (see above) | persons |
| IOM DTM | IDPs present (individuals) per round. A stock at the reporting date. Rounds cover assessed locations only | admin 0 (ISO3), admin 1 (COD-AB p-codes, version as the operation states) | Each round is published once. The acquisition release is declared or dated by `Last-Modified` | individuals |
| BAMF | Erstanträge and Folgeanträge versus Entscheidungen by outcome. Monthly and cumulative periods | Germany | Each monthly report. Later reports revise cumulative figures | persons |
| Destatis | 12411: stock on 31 December, a Fortschreibung on a census base (2011 or 2022). The base is part of the definition. 12711: flows across the German border during the year, by nationality | Germany and Länder by AGS (Gebietsstand of the table; _verify_) | Table `Updated` stamp. A census-base switch is a definition revision and a marked break. Signs: `-` exactly zero, `.` unknown or confidential, `...` not yet available, `x` not applicable, `/` too uncertain | persons |
| Statistik BB | Register-based residents versus census-based population. The base is part of the definition revision | Berlin Bezirke (2001 reform; ALKIS `gem` 001-012). LOR areas not selected | Each file's Stand (declared) | persons |

Release calendars (Eurostat release calendar, Destatis Veröffentlichungskalender)
are recorded per document as `expected_release` evidence only. A release that
is overdue is reported as `release_pending`, never predicted.

## Identifiers retained

- Eurostat dataset codes, filter dimension codes and geo codes.
- UNHCR `coo_iso`/`coa_iso` and UNHCR's own `coo`/`coa` codes.
- DTM operation, p-codes and round numbers.
- GENESIS table codes, Merkmal codes (`DLAND`, `NAT`) and value codes (`BEVSTD`, `BEV081`, `BEV082`).
- Berlin Bezirk codes.

Publisher references to legal acts (for example the ESMS metadata's naming of
Regulation (EC) No 862/2007 and Regulation (EU) No 1260/2013; _verify_) are
declared per document from the metadata page as `references`. They are never
inferred from a title.

## Bounded coverage and fixtures

The `economic-statistics-and-filings` pack (1.3.0) declares five sources:

- `eurostat-demography-migration`: five series.
- `unhcr-refugee-data-finder`: one selection.
- `iom-dtm-idps`: one operation.
- `destatis-genesis-population-migration`: two tables.
- `statistik-bb-bezirke`: two annual files, census base 2011 and 2022.

Each source has an authored fixture under `tests/fixtures/source_packs/`
(bodies under `tests/fixtures/demographics/`). Every value, origin, operation
and admin area in the fixtures is fictional. Fixtures are never evidence of
live coverage.
