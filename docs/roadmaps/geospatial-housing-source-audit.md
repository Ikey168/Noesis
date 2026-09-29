# Geospatial housing: source-contract audit and provider coverage (U01)

Tracking: #1912 · delivery issue #1921 · recorded 2026-09-28.

This audit sets out, per source, what the Geospatial bundle's optional
`housing` feature may acquire, how, and on what terms. It was written without
network access. Endpoints, type names, attribute names, layouts, identifiers
and terms come from the providers' published documentation as the author
knows it. **Every item marked _verify_ must be checked against the live
service, a published response and the current terms before the first dated
live run (U12, #2029). No provider is `live` until that run exists.** The
machine-readable copy of these decisions is `PROVIDER_CONTRACTS` in
`src/ingestion/housing_sources.py`; the MCP tool `housing_source_contracts`
returns it.

These non-goals apply to every source:

- Values are what the publisher released, each with its source, currency,
  unit and valuation date, edition or vintage.
- **No source offers an interpolable value surface.** A land value applies
  inside its published zone for its Stichtag, a rent-index range to its
  published cell for its edition, and a statistic to its reporting area and
  period. Nothing is interpolated, averaged or apportioned between zones,
  cells, reporting areas, sources or editions.
- No property valuation, rent or investment advice and no tenancy legal
  advice is produced or inferred.
- PDF-only and portal-only publications are never scraped.

## Access decisions

| Source | Delivers | Shape | Publisher | Decision | Reason |
| --- | --- | --- | --- | --- | --- |
| FIS-Broker WFS: BORIS Bodenrichtwerte | land-value zones | zone geometry with value | primary (Gutachterausschuss) | `unverified-live` | WFS 2.0.0 GetFeature on `gdi.berlin.de` through the existing `wfs` connector. One layer per Stichtag; the service path, type name (`brw:bodenrichtwertzonen` in the manifest) and attribute names (`wnum`, `brw`, `stag`, `nuta`, `gfz`, `entw`, `beit`, `brzname`, following the VBORIS naming) are _verify_ |
| BORIS Berlin portal | land-value zones | map viewer and per-zone report pages | primary | `not-implemented` | No supported machine access beyond the FIS-Broker WFS of the same publisher (_verify_). The portal is never scraped |
| FIS-Broker WFS: Bebauungspläne | development-plan stages | plan-area geometry with plan number, district, stage and stage dates | primary (SenStadt / Bezirke) | `unverified-live` | Same WFS path. Type name (`bplan:bplan_geltungsbereiche` in the manifest), the stage attribute and its vocabulary (`Aufstellungsbeschluss`, `festgesetzt`...), the date attributes (`afs_beschl`, `festsg_am`) and the reference attributes (`afs_abl`, `gvbl`) are _verify_. Plans in procedure and fixed plans may be separate layers (_verify_) |
| FIS-Broker WFS: Wohnlagen | residential-area categories | block (or address) geometry with the Wohnlage category per Mietspiegel edition | primary | `unverified-live` | Same WFS path, one layer per Mietspiegel edition (`wohnlagen:wohnlagen_2099` in the manifest). Whether the layer serves blocks or address points, and the category attribute (`wol`), are _verify_. A category is stored as the source's label, never translated into a value or score |
| Berliner Mietspiegel | rent-index cells | Mietspiegeltabelle (PDF in the Amtsblatt; online query) | primary | `unverified-live` | No documented machine-readable table (_verify_ daten.berlin.de). The `housing` connector reads a declared CSV layout (`rent-index-table-csv`); for a PDF-only edition this is the operator-extracted table, and the edition, qualifying date, publication URL and page stay on the declaration. The PDF is never scraped |
| Amt für Statistik Berlin-Brandenburg | permit and completion statistics | download tables per Bezirk and period; Statistische Berichte F II (PDF) | primary | `unverified-live` | Download tables in a declared column layout (`statbb-building-csv`): Bezirk code, name, period and value columns. A Land total row is declared (`total_codes`), never inferred. File URLs and columns are _verify_. PDF reports are not scraped |
| Destatis GENESIS-Online, tables 31111 and 31231 | housing indicators (permits, completions by Land) | tabular series | primary | `unverified-live` (credentialed) | REST API 2020 through the new dataset connector `src/ingestion/connectors/dataset/genesis.py`: `metadata/table` for the table's `Updated` stamp (the vintage) and `data/tablefile?format=ffcsv` for values. Credentials (token, secret `NOESIS_DESTATIS_GENESIS_TOKEN`) go in request headers (the login requirement and header names after the 2024 authentication change are _verify_). Table codes (31111-0004, 31231-0003 in the manifest) and value codes are _verify_. Observations land in the existing `ObservationStore` |
| IBB Wohnungsmarktbericht | cited publication | annual PDF report | aggregator (of Statistik BB, Mietspiegel and market data) | `not-implemented` | PDF-only secondary publication. Recorded as a cited publication only, never scraped or extracted into values. Its offer rents are asking rents, not rent-index values |
| daten.berlin.de | catalogue | CKAN entries pointing to the publishers' services and files | aggregator | `not-implemented` | Used to find the publishers' own resources; every housing value is acquired from its primary publisher |

**Unavailable-access fallback.** A publication or feature is not stored as a
housing record when any of these happens: an HTTP error, a redirect to
another host, a missing credential, an exhausted budget, schema drift, an
undeclared column, unit or stage label, or a publication that states no
publication date (a retrieval time never poses as one). A tabular
publication is all-or-nothing and the source run fails visibly. A WFS
feature whose housing attributes are missing or unreadable keeps its geometry
in the Geospatial store and gets a `housing_projection_outcomes` row with the
reason; it is never dropped silently. Records already stored stay as they
are.

## Per-source contract

### FIS-Broker WFS layers (BORIS, Bebauungspläne, Wohnlagen)

- **Access:** WFS 2.0.0 `GetFeature` with `OUTPUTFORMAT=application/json`,
  `SRSNAME=urn:ogc:def:crs:EPSG::25833`, a stable `SORTBY` and
  `COUNT`/`STARTINDEX` paging bounded by the declared page size and budgets;
  `numberMatched` ends paging. This is the path the ALKIS districts, schools
  and Umweltatlas layers already use.
- **Authentication:** none. **Rate limits:** undocumented (_verify_).
- **CRS:** ETRS89 / UTM 33N (EPSG:25833), projected to WGS84 at acquisition
  with the transform receipt kept on the feature revision.
- **Identifiers:** the WFS feature id is the Geospatial feature identity; the
  published zone number (Bodenrichtwertnummer), plan number (Planname) or
  block id is the housing identity.
- **Dates:** BORIS carries its Stichtag per zone; a missing Stichtag stays
  unknown. Plans carry the stage and the date of each stage as published;
  a missing date stays unknown. Wohnlagen carry no date: the Mietspiegel
  edition and its `valid_from` are declared per layer.
- **Currency and units:** BORIS values are EUR per square metre of land
  (declared `EUR/m²`, currency `EUR`) with Art der Nutzung, GFZ,
  Entwicklungszustand and beitragsrechtlicher Zustand kept as published
  qualifiers.
- **Cadence:** BORIS yearly (Stichtag 1 January, published in spring);
  plans continuous; Wohnlagen per Mietspiegel edition.
- **Terms:** Datenlizenz Deutschland – Zero 2.0 for Geoportal Berlin open
  data (_verify_ for the BORIS and Wohnlagen layers).

### Berliner Mietspiegel

- **Delivers:** the Mietspiegeltabelle of each edition: cells by Wohnlage,
  Baualter and Wohnfläche, each with lower, middle and upper values in EUR per
  square metre of living space per month (net cold rent).
- **Dates:** each edition states its qualifying date (Stichtag der
  Datenerhebung) and the date it applies from. An edition never overwrites
  another. The edition's publication date is its vintage.
- **Identifiers:** edition (for example "Berliner Mietspiegel 2023") and the
  published cell key.
- **Cadence:** every two years (qualified Mietspiegel), with interim updates.
- **Terms:** official publication, reuse with source attribution (_verify_).

### Amt für Statistik Berlin-Brandenburg

- **Delivers:** building permits (Baugenehmigungen) and completions
  (Baufertigstellungen): dwellings and floor area (1000 m²) per Bezirk
  (codes 01–12, the ALKIS `gem` 001–012) and period.
- **Dates:** reporting period per row; the publication date (declared, or
  `Last-Modified`) is the vintage. Revised figures for a period are a new
  vintage; earlier vintages stay.
- **Terms:** CC BY 3.0 DE / Datenlizenz Deutschland (_verify_ per table).

### Destatis GENESIS-Online (31111, 31231)

- **Delivers:** building permits (31111) and completions (31231) by Land
  (`DLAND`, AGS Land code) and year.
- **Dates:** the table's `Updated` stamp (Stand) is the vintage; `Zeit` is the
  reference year. A republication with other values under the same stamp is
  refused as a silent change; a relabelled table under the same stamp is a
  recorded correction.
- **Signs:** GENESIS Zeichenerklärung (`-` exactly zero, `.` unknown or
  confidential, `...` not yet available, `x` not applicable, `/` too
  uncertain) is kept per period.
- **Terms:** Datenlizenz Deutschland – Namensnennung 2.0 (_verify_).

## Fixtures

Every housing source in `geospatial-berlin` 1.3.0
(`packs/geospatial/source_packs/geospatial-berlin-1.3.0.json`) pins an
*authored* fixture in the documented native shape
(`tests/fixtures/source_packs/geospatial-housing-*.json`). Each declares its
source URL, retrieval note and licence. Values, identifiers, plan numbers and
dates (2097–2101) are fictional. Geometries are boxes in EPSG:25833. The
builder is `tests/unit/housing_fixture_builder.py`.

## Pack version

The issue asks for the sources in `config/source_packs/geospatial.json` with
a version bump. That file is the installed 1.1.0 base that the Climate and
Environment bundle upgrades to 1.2.0 (Umweltatlas layers) from its own
manifest. Bumping the base would turn that upgrade into a downgrade. The
housing sources therefore ship as the next upgrade of the same pack,
`geospatial-berlin` 1.3.0: the 1.2.0 sources verbatim plus the six housing
sources, applied through the source-pack upgrade path. **Needs a maintainer
decision:** whether to fold 1.2.0 and 1.3.0 into the base file.
