# Geospatial real estate: source contracts and bounded coverage (RE01, #2460)

Tracking issue: #2228. This audit is recorded against the existing Geospatial
pack, `config/source_packs/geospatial.json` and the housing layer (#1912). It
backs the machine-readable decisions in `src/ingestion/real_estate_sources.py`
(`PROVIDER_CONTRACTS`, `REUSE_CONDITIONS`, `BOUNDED_COVERAGE`,
`LIVE_VERIFICATION`).

Out of scope throughout: property valuation, price estimates, owner or party
profiling beyond publisher data, re-identification of individuals and
investment advice.

## Status of the evidence

Every entry below was written from the publishers' documentation without
network access. Endpoints, file layouts, WFS type names and terms marked
**verify** must be checked against the live services before the dated live run
(RE13, #2519). Until then every provider is `unverified-live` and all tests run
on authored, fictional fixtures (`tests/fixtures/source_packs/real-estate-*.json`).

## Where the sources ship

The sources ship in the separate `geospatial-real-estate` 1.0.0 source pack
(`packs/geospatial/source_packs/geospatial-real-estate.json`), pinned by the
`geospatial.real-estate` provider. They are **not** added to
`config/source_packs/geospatial.json`: that file is the `geospatial-berlin`
1.1.0 base that the Climate and Environment 1.2.0 and housing 1.3.0 upgrades
keep verbatim, and adding sources there would change every upgrade preview.
The separate pack is the same arrangement the Climate and Environment
biodiversity feature uses.

## Source contracts

| Source | Access | Format | Identifiers | Licence and attribution | Rate limits | Cadence and revisions |
| --- | --- | --- | --- | --- | --- | --- |
| HM Land Registry Price Paid Data (`hmlr-ppd`) | HTTPS monthly change file `pp-monthly-update-new-version.csv` (verify host) | CSV, 16 columns, no header | transaction unique identifier `{GUID}`; postcode | Open Government Licence v3.0 with the HM Land Registry attribution | none published; one file per month | monthly; rows carry record status `A` (addition), `C` (change), `D` (deletion); the file's `Last-Modified` month is the release |
| UK House Price Index (`hmlr-ukhpi`) | HTTPS full file per monthly release (verify path `UK-HPI-full-file-YYYY-MM.csv`) | CSV with header | ONS GSS area codes; month | OGL v3.0 | none published | monthly; recent months revised in later releases; index Jan 2015 = 100 |
| DVF géolocalisées (`dvf`) | `files.data.gouv.fr/geo-dvf/{release}/csv/{year}/communes/{dep}/{commune}.csv` (verify) | CSV with header, one row per disposition, parcel and local | `id_mutation`; `id_parcelle` (14 characters: INSEE commune, prefix, section, number) | Licence Ouverte 2.0 with the DVF conditions below | none published | semi-annual (April, October), last five years |
| Eurostat house price index (`eurostat-hpi`) | dissemination API `prc_hpi_q`, JSON-stat 2.0, through the existing `EurostatConnector` | JSON-stat cube | dataset code, GEO, purchase, unit | Eurostat reuse policy (Decision 2011/833/EU), attribution | undocumented soft limits (verify) | quarterly; revised quarters; flags `p`, `e`, `b`; rebasing changes the unit code and label |
| INSPIRE Cadastral Parcels, France (`inspire-cp-fr`) | WFS 2.0.0 `data.geopf.fr/wfs/ows`, type `CP.CadastralParcel` (verify) | GeoJSON output | INSPIRE `localId` + `namespace`; `nationalCadastralReference` = `id_parcelle` | Licence Ouverte 2.0 (verify for the layer) | undocumented; COUNT/STARTINDEX paging | quarterly to semi-annual (verify) |
| INSPIRE Cadastral Parcels, Nordrhein-Westfalen (`inspire-cp-de-nw`) | WFS 2.0.0 `www.wfs.nrw.de/geobasis/wfs_nw_inspire-flurstuecke_alkis`, type `cp:CadastralParcel` (verify) | GeoJSON output (verify availability) | `localId`, `namespace`, `nationalCadastralReference` (Flurstückskennzeichen) | Datenlizenz Deutschland Zero 2.0 | undocumented | continuously updated cadastre |
| INSPIRE Cadastral Parcels, Netherlands (`inspire-cp-nl`) | WFS on PDOK | GML/GeoJSON | `localId`, `nationalCadastralReference` | CC BY 4.0 (verify) | - | **not selected**: no linked transaction source in the bounded countries |
| HM Land Registry INSPIRE Index Polygons | per-authority GML zip | GML, EPSG:27700 | INSPIRE ID only | INSPIRE Index Polygons licence (verify) | - | **not implemented**: no WFS path, no identifier shared with PPD, and the bulk download exceeds the RE04 boundary |

## Per-country INSPIRE services and CRS

* **France** - IGN Géoplateforme WFS. Requested as ETRS89 / UTM zone 31N
  (`EPSG:25831`) so the offline projector needs no pyproj; the service's native
  CRS is Lambert-93 (`EPSG:2154`), which needs the optional pyproj dependency
  (verify that the service offers `EPSG:25831`). Atom downloads exist per
  département but are not used (no bulk download beyond the RE01 bounds).
* **Nordrhein-Westfalen** - Geobasis NRW INSPIRE WFS in its native
  ETRS89 / UTM zone 32N (`EPSG:25832`).
* Geometries are kept in the source CRS in the Geospatial feature store with
  the coordinate transform recorded; the WGS84 geometry is a projection with
  its receipt.

Both parcel sources pin a bounding box and a `PROPERTYNAME` list (`localId`,
`namespace`, `nationalCadastralReference`, `label`, `areaValue`,
`beginLifespanVersion`). Any other property a service returns - a rights
holder included - is dropped in the WFS adapter before a record reaches the
runtime; the page receipt names what was dropped, never its value.

## Reuse conditions affecting individuals

* **PPD** - addresses of sold properties are published; no buyer, seller or
  owner is. The PPD terms (verify) restrict using address data to contact or
  profile individuals. Record design: addresses are stored as published for
  place matching only; no party field exists or is accepted; no query takes a
  person's name.
* **DVF** - no names are published, but transactions are re-identifiable
  through address and parcel. The DVF conditions (décret 2018-1350, verify)
  forbid **re-identification** of the persons concerned and indexing by
  external search engines. Record design: no name is stored; answers are by
  place code or parcel identifier only; every answer containing DVF data carries
  the no-re-identification notice; nothing is published for indexing.
* **INSPIRE CP** - owner and rights-holder attributes are never requested or
  kept (see above).

A tabular file that publishes an owner, buyer, seller or rights-holder column
is refused (`party_column`), and a statement carrying such a field or a
valuation is refused by the record store (`forbidden_field`).

## Bounded coverage for live verification

| Source | Places | Periods | Sample |
| --- | --- | --- | --- |
| PPD | the postcode districts of one London borough (fixtures use the fictional `ZZ1`, `ZZ2`) | the monthly change files of the verification window | every row of the declared districts |
| UK HPI | the borough (GSS) and England (`E92000001`) | the last 24 months of two releases | index, average price, sales volume |
| DVF | Paris 4e arrondissement (INSEE `75104`) | one year in two semi-annual releases | every mutation of that commune-year |
| Eurostat HPI | FR, DE | quarters since the declared start; unit `I15_Q`, purchase `TOTAL` | - |
| INSPIRE CP France | a bounding box in Paris 4e (`EPSG:25831`) | current | at most 50 parcels, including those DVF mutations name |
| INSPIRE CP NRW | a bounding box in Köln-Altstadt (`EPSG:25832`) | current | at most 50 parcels |

Budgets per source are in the source pack (bytes, pages, results, timeout); a
publication with more rows than the run's result budget is refused rather than
truncated, so a missing row never reads as a withdrawn transaction.
