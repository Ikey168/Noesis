# Migration and demographic statistics expansion scope

Status: planned, 2026-09-27. An expansion of the existing Economics bundle; it
does not create a separate domain or pack. It never produces a population
projection, a causal claim about migration drivers, or a merged series across
different definitions, publishers or geography levels.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1924–#2023 are open.
- Composition dependency: an `economics.demographics` provider descriptor and
  optional `demographics` feature in the Economics bundle (`packs/economics/`),
  composed with `economics.core` (`economics.knowledge`), `legal.works`,
  `political.knowledge`, `geospatial.feature-query`,
  `geospatial.place-resolution`, `platform.subscriptions` and
  `platform.source-acquisition`. The feature must coexist with the planned
  `economics.public-finance` feature; both are optional and default to false.

Tracking: [#1914](https://github.com/Ikey168/Noesis/issues/1914).

## Outcome

Given a geography and a period, assemble population and migration series with
their definitions (stock versus flow, citizenship versus country of birth,
asylum applications versus decisions), vintages and geography level from
statistical offices and UN agencies. Regional series are projected onto the
boundaries the Geospatial store already holds, and series are linked by
citation to the legal acts, court decisions and legislative dossiers that
Legal and Political already hold. Series from different publishers stay
separate with explicit comparability notes; breaks in series stay marked, and
missing definitions stay unknown.

## GitHub implementation issues

- [ ] [#1924](https://github.com/Ikey168/Noesis/issues/1924) — M01 Audit migration and demographic source contracts and select bounded provider coverage.
- [ ] [#1931](https://github.com/Ikey168/Noesis/issues/1931) — M02 Define series, definition-revision, geography-level and indicator-vintage records for population and migration statistics.
- [ ] [#1942](https://github.com/Ikey168/Noesis/issues/1942) — M03 Acquire Eurostat migration and demography series through the existing SDMX connector with vintages.
- [ ] [#1951](https://github.com/Ikey168/Noesis/issues/1951) — M04 Acquire UNHCR and IOM series with definitions and coverage notes preserved.
- [ ] [#1960](https://github.com/Ikey168/Noesis/issues/1960) — M05 Acquire BAMF, Destatis and Berlin statistics as vintaged series.
- [ ] [#1969](https://github.com/Ikey168/Noesis/issues/1969) — M06 Record comparability across publishers without merging series.
- [ ] [#1978](https://github.com/Ikey168/Noesis/issues/1978) — M07 Project regional series onto Geospatial boundaries and answer as-of queries.
- [ ] [#1988](https://github.com/Ikey168/Noesis/issues/1988) — M08 Link series to legal acts, court decisions and legislative dossiers by citation.
- [ ] [#1997](https://github.com/Ikey168/Noesis/issues/1997) — M09 Monitor new releases and vintage revisions through subscriptions.
- [ ] [#2007](https://github.com/Ikey168/Noesis/issues/2007) — M10 Register the `economics.demographics` provider descriptor, the `migration` profile and optional feature in the Economics bundle.
- [ ] [#2017](https://github.com/Ikey168/Noesis/issues/2017) — M11 Add offline geography-to-series-and-definitions acceptance coverage.
- [ ] [#2023](https://github.com/Ikey168/Noesis/issues/2023) — M12 Validate live coverage and publish a cited demographic dossier demo.

Order: M01 → M02 → {M03, M04, M05} → M06 → M07 → M08 → M09 → M10 → M11 → M12.

## Sources

- [Eurostat migration and asylum database](https://ec.europa.eu/eurostat/web/migration-asylum/database):
  immigration and emigration flows, asylum applications and decisions, through
  the existing SDMX connector.
- [Eurostat population and demography database](https://ec.europa.eu/eurostat/web/population-demography/database):
  population on 1 January by citizenship and country of birth, NUTS levels.
- [UNHCR Refugee Data Finder](https://www.unhcr.org/refugee-statistics/):
  API; population types (refugees, asylum-seekers, IDPs, stateless) as
  separate definitions with coverage notes.
- [IOM DTM data and analysis](https://dtm.iom.int/data-and-analysis):
  displacement figures; machine access to be confirmed by M01.
- [BAMF asylum figures](https://www.bamf.de/DE/Themen/Statistik/Asylzahlen/asylzahlen-node.html):
  applications and decisions; machine-readable access to be confirmed by M01,
  PDF-only publications are not scraped.
- [Destatis GENESIS](https://www-genesis.destatis.de/): tables 12411
  (population by Land, age, sex and nationality) and 12711 (migration between
  Germany and abroad); credentialed API access.
- [Statistik Berlin-Brandenburg](https://www.statistik-berlin-brandenburg.de/):
  Berlin district population and migration statistics; access to be confirmed
  by M01.

M01 verifies the documented access method, terms, authentication, rate limits,
identifiers, definitions and cadence per provider and records an
`unverified-live` or `not-implemented` decision for each. These links establish
source candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Economics:** the host bundle. `src/kb/demographics.py` owns series,
definition-revision, geography-level, vintage, comparability and citation-link
records, reusing the `economic_vintages` release and vintage pattern from
`src/domains/economic/model.py` and the release-cutoff selection of
`src/domains/economic/releases.py`. Acquisition goes through the existing
`src/ingestion/connectors/dataset/{sdmx,eurostat,worldbank}.py` connectors and
new source entries in `config/source_packs/economic.json`. The bundle gains an
`economics.demographics` provider descriptor, an optional `demographics`
feature and a `migration` profile; `kb_economic` and the existing release and
vintage tools are extended, not duplicated. Definitions are first-class
records: a change of definition is a new definition revision and a marked
break, never a re-based value.

**Legal:** series and definitions link to `legal_works` works and decisions in
`src/kb/legal.py` only through an explicit publisher reference (a regulation
or directive named in dataset metadata, a legal basis cited in a release) or a
reviewed assertion, with the cited passage locator retained. A link never
asserts that a series measures an act's effect or that an act is in force.

**Political:** the same citation links join series to legislative dossiers in
`src/domains/political/legislative_dossiers.py` and to `kb_political`
knowledge. A shared keyword, date or topic is at most a discovery candidate
and is marked as such.

**Geospatial:** NUTS, AGS and Berlin Bezirk codes are resolved to existing
places and boundary features through `src/kb/geospatial.py` and
`geospatial_features.py`, with the code-list version and boundary vintage
recorded. Series at another geography level are listed as available at that
level, never aggregated or apportioned to the requested boundary; unresolved
codes stay unresolved.

**Units:** persons, per-thousand rates and reference periods go through the
existing pint-based normalisation with the source unit retained; conversions
produce calculation receipts, never stored series.

**Subscriptions and source runtime:** releases, vintage revisions, definition
revisions and series breaks are delivered through `src/kb/subscriptions.py`
and acquired through the source-pack runtime, with no new scheduler or
delivery path.

**Rights and terms:** publisher terms (Eurostat reuse, UNHCR, IOM, Destatis
and Berlin licences) are recorded per source in the source pack; embargoed or
confidential values stay flagged as published, and the expansion redistributes
nothing beyond what the terms allow.

## V1 acceptance

- Pinned fixtures for Eurostat, UNHCR, IOM, BAMF, Destatis and Berlin cover
  stock and flow series, citizenship and country-of-birth breakdowns,
  applications and decisions, provisional and break flags, a definition change
  and two publishers' series for one concept.
- Repeated ingestion is idempotent; every value carries publisher, dataset or
  table code, definition revision, unit, geography level and vintage.
- A query for a geography and period returns each publisher's series side by
  side with comparability notes; no merged or averaged cross-publisher value
  exists, and a pair without a note reports `comparability_unknown`.
- As-of queries select the vintage released on or before the requested date,
  report `historical_vintage_unavailable` when none is retained, and mark a
  pinned vintage `stale` when a later one arrives.
- Citation links to legal works and legislative dossiers exist only from
  explicit references or reviewed assertions and are reversible.
- The `demographics` feature resolves alone, together with the planned
  `economics.public-finance` feature, and unselected; the Economics bundle is
  unchanged when it is off.
- Offline replay and bounded live checks are recorded separately per provider
  under `docs/development/demographics-evidence/`. The demo answers a Berlin
  district and period question with series, definitions, vintages,
  comparability notes, boundary projection and legal links, and makes no
  projection or causal statement.

## Deferred

Defer population projections and scenarios, causal or attribution analysis of
migration drivers, harmonised or chained cross-publisher series, small-area
estimation below the lowest published geography level, additional national
statistical offices, microdata access and any scraping of portal-only or
PDF-only publications. Publisher record counts do not imply comparable
definitions or complete coverage.

There is no standalone Migration and demographic statistics domain manifest,
source-pack manifest, enablement flag, or separate installation lifecycle in
this scope.
