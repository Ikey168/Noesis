# Housing expansion scope

Status: planned, 2026-09-27. An expansion of the existing Geospatial bundle; it
does not create a separate domain or pack. It produces no property valuation,
no rent or investment advice, no legal advice on tenancy and no interpolation
of values between zones.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1921–#2029 are open.
- Composition dependency: a `geospatial.housing` provider descriptor and
  optional `housing` feature (default false) in the Geospatial bundle
  (`packs/geospatial/`), composed with `geospatial.core`, `geospatial.transit`
  (accessibility context only), `legal.works`, `economics.knowledge` and the
  dataset connectors, `political.knowledge`, `news.articles`,
  `platform.subscriptions` and `platform.source-acquisition`.

Tracking: [#1912](https://github.com/Ikey168/Noesis/issues/1912).

## Outcome

Given an address, parcel or district, assemble source-cited housing evidence:
land-value zones with valuation dates, rent-index cells and editions,
development plans and their procedural stage, permit and completion
statistics, and housing indicator vintages, projected onto places with as-of
provenance and linked by citation to legal works and district decisions.
Sources, revisions, valuation dates and unknowns stay visible; conflicting
assertions are kept side by side, never merged.

## GitHub implementation issues

- [ ] [#1921](https://github.com/Ikey168/Noesis/issues/1921) — U01 Audit housing, land-value and planning source contracts and select bounded provider coverage.
- [ ] [#1937](https://github.com/Ikey168/Noesis/issues/1937) — U02 Define land-value-zone revision, rent-index cell, development-plan stage, permit-statistic and housing-indicator-vintage records.
- [ ] [#1945](https://github.com/Ikey168/Noesis/issues/1945) — U03 Acquire BORIS land-value zones through the existing WFS source path with valuation dates.
- [ ] [#1955](https://github.com/Ikey168/Noesis/issues/1955) — U04 Acquire Berlin development plans and residential-area layers through the existing WFS source path with procedural stage.
- [ ] [#1966](https://github.com/Ikey168/Noesis/issues/1966) — U05 Acquire Mietspiegel editions and permit and completion statistics as vintaged series.
- [ ] [#1976](https://github.com/Ikey168/Noesis/issues/1976) — U06 Acquire Destatis housing series through the existing dataset connectors with vintages.
- [ ] [#1986](https://github.com/Ikey168/Noesis/issues/1986) — U07 Project zones, plans and statistics onto places and answer as-of spatial queries.
- [ ] [#1995](https://github.com/Ikey168/Noesis/issues/1995) — U08 Link rent-index cells and development plans to legal works and district decisions by citation.
- [ ] [#2005](https://github.com/Ikey168/Noesis/issues/2005) — U09 Monitor new land-value publications, plan-stage changes and rent-index editions through subscriptions.
- [ ] [#2015](https://github.com/Ikey168/Noesis/issues/2015) — U10 Register the `geospatial.housing` provider descriptor and optional feature in the Geospatial bundle.
- [ ] [#2022](https://github.com/Ikey168/Noesis/issues/2022) — U11 Add offline address-to-housing-dossier acceptance coverage.
- [ ] [#2029](https://github.com/Ikey168/Noesis/issues/2029) — U12 Validate live coverage and publish a cited housing-dossier demo.

Order: U01 → U02 → {U03, U04, U05, U06} → U07 → U08 → U09 → U10 → U11 → U12.

## Sources

- [FIS-Broker](https://fbinter.stadt-berlin.de/fb/index.jsp): Berlin WFS
  layers for BORIS Bodenrichtwerte, Bebauungspläne and Wohnlagen, acquired
  through the existing `wfs` connector of the `geospatial-berlin` source pack.
- [BORIS Berlin](https://www.boris-berlin.de/): the land-value publication
  and its valuation dates (Stichtage).
- [Berlin Mietspiegel](https://www.stadtentwicklung.berlin.de/wohnen/mietspiegel/):
  rent-index editions and their cells; the access path (table, PDF or data
  file) is decided in U01.
- [Amt für Statistik Berlin-Brandenburg](https://www.statistik-berlin-brandenburg.de/):
  building permits (Baugenehmigungen) and completions (Baufertigstellungen)
  per reporting area and period.
- [Destatis GENESIS](https://www-genesis.destatis.de/): tables 31111 and
  31231 through the existing dataset connectors, with publication vintages.
- [IBB Wohnungsmarktbericht](https://www.ibb.de/de/ueber-uns/publikationen/wohnungsmarktbericht/):
  a cited publication unless U01 finds a supported data access path.
- [daten.berlin.de](https://daten.berlin.de/): catalogue entries and licences
  for the Berlin datasets above.

U01 verifies documented access, terms, authentication, rate limits,
identifiers, coordinate reference systems and cadence per provider and records
an `implemented`, `unverified-live` or `not-implemented` decision for each.
These links establish source candidates, not guaranteed APIs or complete live
coverage.

## Expansion of existing capabilities

**Geospatial:** BORIS zones, development plans and residential areas are
acquired through `src/ingestion/wfs_api.py` and `geojson_features.py` as new
entries in `config/source_packs/geospatial.json`, and their geometries live in
`geospatial_features` and `geospatial_geometries` (`src/kb/geospatial.py`,
`geospatial_features.py`). `src/kb/housing.py` owns only the value, stage,
cell and statistic records and references geometries by identifier. Address
and parcel inputs go through the reviewable place resolution; containment and
proximity use `features-within` and `calculate-spatial-relation`. Transit
stops from `src/kb/transit.py` appear as accessibility context and carry no
valuation meaning. A point on a zone boundary or outside every published zone
is reported as such.

**Legal:** rent-index editions and development-plan stages link to Berlin
legal works and passages through `legal_citations` in `src/kb/legal.py`, using
the `berlin-law-publications` source already in the Legal pack. Only explicit
citations become links; a shared place or keyword is at most a reviewable
candidate. Nothing is summarised into tenancy advice.

**Economics and datasets:** Destatis GENESIS series are acquired through
`src/ingestion/connectors/dataset/` into the existing `ObservationStore` with
publication vintages; housing records reference the series rather than copying
the numbers. Units are labelled through the pint normalisation in
`src/integrations/units.py`; monetary values are never converted.

**Political and News:** district and Senate decisions that set a plan's stage
come from `political.knowledge`; `news.articles` items are discovery context
with their source, not evidence of a stage or value.

**Platform:** subscriptions on new valuation dates, plan-stage changes and
rent-index editions use `SubscriptionStore` (`src/kb/subscriptions.py`) and
acquisition runs through `platform.source-acquisition`
(`src/ingestion/source_pack_runtime.py`). No scheduler, permission ledger,
project store, entity store or spatial store is added.

**Rights and terms:** Berlin layers are published under Datenlizenz
Deutschland where the catalogue says so; Mietspiegel, IBB and statistical
publications keep their own terms. Each source entry records its licence and
attribution, and a publication with unclear reuse terms is cited by link
rather than redistributed.

## V1 acceptance

- Pinned native fixtures cover a BORIS zone across two valuation dates, a
  development plan whose stage changed between snapshots, a Wohnlage
  category, a Mietspiegel edition with its cells, a permit and completion
  series with a revised period, and a Destatis series with two vintages.
- A fixture address resolves to a place and yields a dossier whose every item
  shows source, currency, unit and as-of basis (valuation date, edition or
  vintage); as-of selection and revision comparison are deterministic and
  replayable.
- Conflicting figures for the same zone, cell or period stay side by side;
  boundary points and points in no zone are reported; no value is
  interpolated, averaged or converted.
- Citation links to legal works and district decisions exist only from
  explicit citations; similarity matches remain reviewable candidates.
- Repeated acquisition is idempotent, and monitoring emits an event only for
  a new source revision, recovering from watermarks after a restart.
- The `housing` optional feature composes in the Geospatial bundle with
  OSINT and Science bindings unchanged, and the composition tests pass with
  the feature disabled and enabled.
- Offline evidence (`tests/unit/domains/test_housing_acceptance.py` and the
  demo) and bounded live evidence (`docs/development/housing-evidence/`) are
  reported separately, and nothing is marked verified without a dated run.

## Deferred

Defer property valuation and comparables, rent or investment guidance,
tenancy legal assessment, value interpolation or smoothing between zones,
coverage beyond Berlin and the two Destatis tables, parcel-level ALKIS
ownership data, scraping of the FIS-Broker web client where no WFS layer
exists, and a map editing or planning interface. Statistical counts describe
reporting areas and periods; they do not describe an individual property.

There is no standalone Housing domain manifest, source-pack manifest,
enablement flag, or separate installation lifecycle in this scope.
