# Places and geometry: second-publisher depth audit (DG01)

Tracking: [#2829](https://github.com/Ikey168/Noesis/issues/2829) DG01 (wave 4
tracker [#2825](https://github.com/Ikey168/Noesis/issues/2825)) · decision
[ADR-006](../../architecture/decisions/ADR-006-source-depth.md) · plan
[source depth program](../../roadmaps/source-depth-program.md) · recorded
2026-11-01.

`places-geometry` is thin: its only provider, `geospatial.core`, reads one
publisher, Land Berlin (Geoportal Berlin WFS layers `berlin-bezirksgrenzen`,
`berlin-schulen`, `umweltatlas-umweltzone`, `umweltatlas-laerm-lden`,
`umweltatlas-klimafunktion` in the `geospatial-berlin` pack; WFS 2.0.0 connector
`src/ingestion/wfs_api.py`; features in `src/kb/geospatial_features.py`,
geometries `noesis-geospatial-geometry-v2` and places
`noesis-geospatial-place-v1` in `src/kb/geospatial.py`).

**It was written without network access.** The publisher hosts
(`gisco-services.ec.europa.eu`, `ec.europa.eu`, `gdz.bkg.bund.de`,
`daten.gdz.bkg.bund.de`, `sgx.geodatenzentrum.de`, `www.openstreetmap.org`,
`overpass-api.de`, `download.geofabrik.de`) are blocked by this runtime's egress
proxy (verified 2026-11-01). Terms, endpoints, layer names and vintages come from
the publishers' documentation as the author knows it and from what other Noesis
sources record. Every item marked _verify_ must be checked against the live
pages, the live terms and a real response before the first dated live run. None
of the sources below is live; none counts toward live depth (ADR-006,
decision 6).

## Requirement: two geometries for the same Berlin place

The second publisher must cover Berlin, so that one place carries two
geometries, each with its own publisher, vintage and precision, and their
disagreement stays visible. The store already guarantees the rest:
features are identified by provider + collection + native id and "are never
merged across providers automatically" (`src/kb/geospatial_features.py`);
geometries are content-addressed and keep `source`, `evidence` and
`valid_from_ms`/`valid_to_ms`. The delivery must add nothing that merges,
snaps, unions, dissolves or edge-matches one publisher's geometry to the
other's. A place gets a second geometry only through a reviewed match on a
published code (AGS/ARS, NUTS, LAU), code before name, as
`src/kb/tourism_identity.py` does for NUTS codes.

**Which Berlin place.** Land Berlin today supplies the 12 districts
(`alkis_bezirke:bezirksgrenzen`), not an outline of Berlin as a whole. The
official candidates below supply Berlin as a whole (Land = Kreis = Gemeinde,
AGS `11000000`; NUTS `DE3`/`DE30`/`DE300`; one LAU) and no district polygons
(_verify_ for each). The union of the 12 districts is never computed to stand in
for Berlin: that would be a derived geometry. So the delivery has two options,
to be settled in the delivery issue:

- (a) add Land Berlin's own state-boundary layer (an ALKIS Land Berlin or
  Landesgrenze WFS layer on `gdi.berlin.de`, _verify_ that it exists) so Berlin
  as a whole has a Land Berlin geometry and a second-publisher geometry; or
- (b) if no such layer exists, compare at feature level only: each district
  boundary is tested against the second publisher's Berlin outline with the
  existing containment receipts, so edge differences appear as points outside,
  never corrected.

Option (a) is the one that meets the requirement as written; option (b) leaves
the requirement partly open and the audit's recommendation then says so.

## Origin chain (ADR-006, decision 5)

German administrative boundaries originate in the surveying and cadastral
authorities of the Länder (for Berlin: the Berlin cadastre, ALKIS, which is also
the source of Geoportal Berlin's `alkis_bezirke` layer). BKG compiles the
Länder's data, through the AdV, into VG250 and the other administrative
boundary products. EuroGeographics builds its pan-European boundary products
from national mapping and cadastral agencies (BKG for Germany), and Eurostat's
GISCO derives the NUTS and LAU boundary sets from EuroGeographics data
(_verify_ the current GISCO lineage statement). OpenStreetMap boundaries are
contributed by volunteers; in Germany many administrative boundaries were
imported from, or traced against, official data that the public authorities
permitted for OSM (_verify_ for Berlin's relations).

So: BKG and GISCO each add a publisher; **neither adds an origin independent of
Land Berlin** for the Berlin boundary line. Their geometries do differ by
generalisation (VG250 at 1:250,000; GISCO at 1:1M, 1:3M, 1:10M, 1:20M and
1:60M), vintage and processing, and that difference is what the second
geometry is meant to show. OSM may be an independent origin in parts, but its
provenance per boundary is not established. Record-level independence stays
with the
[Evidence Independence Graph](../../../contracts/noesis-evidence-independence-v1.md);
two geometries of one boundary never corroborate each other.

## Summary

| Candidate | Publisher | Registry entry | Covers Berlin | Independent origin | Shape fit | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| BKG VG250 (WFS or download) | Bundesamt für Kartographie und Geodäsie | new (`bkg`) | Berlin as a whole (Land, Kreis, Gemeinde `11000000`); no districts (_verify_) | no (compiled from Länder data) | fits the WFS feature path and `noesis-geospatial-geometry-v2` directly | **recommended** for `geospatial.core` |
| Eurostat GISCO NUTS / LAU | Eurostat | existing (`eurostat`) | Berlin as a whole (`DE3`, `DE30`, `DE300`; one LAU) (_verify_) | no (EuroGeographics from BKG) | fits through the GeoJSON feature decoder; NUTS version is an explicit vintage | `not-implemented` this wave: licence clause on administrative boundaries unverified; second candidate once verified |
| OpenStreetMap administrative boundaries | OpenStreetMap contributors | existing (`openstreetmap`, used by `geospatial.infrastructure`) | Berlin and its 12 districts (`admin_level` 4 and 9, _verify_) | unclear per relation | needs ring assembly from ways (our own construction) | `not-implemented` for `geospatial.core`: ODbL share-alike on the derived database, and ring assembly |

## Per-candidate contract

### BKG VG250 (Verwaltungsgebiete 1:250 000)

| Item | Record |
| --- | --- |
| Endpoint | WFS `https://sgx.geodatenzentrum.de/wfs_vg250` (layers per level, e.g. `vg250:vg250_lan`, `vg250:vg250_krs`, `vg250:vg250_gem`, _verify_ service URL, version 2.0.0 support, layer names and a GeoJSON output format); downloads `https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_1231/` (GeoPackage or Shapefile, UTM32s / EPSG:25832, as of 31 December of each year, _verify_ paths) |
| Access | none (open data) (_verify_) |
| Licence | Datenlizenz Deutschland - Namensnennung - Version 2.0 (`dl-de/by-2-0`) (_verify_) |
| Redistribution | permitted, including in derived works and evidence bundles, with the attribution BKG prescribes ("© GeoBasis-DE / BKG <year>", _verify_ wording) and a link to the licence |
| Vintage | `vg250_1231` is the state of 31 December (one vintage per year); `vg250_0101` the state of 1 January (_verify_). The vintage is declared per document and stored as the geometry's valid-time basis, never inferred from retrieval time |
| Codes | ARS/AGS per feature (`ARS`, `AGS`, `GEN`, `BEZ`, `GF`, _verify_ attributes); Berlin AGS `11000000`; `GF` distinguishes land areas from water/coastal parts (_verify_) |
| Precision | scale 1:250,000; positional accuracy as stated in the product documentation (_verify_), recorded as `declared` precision if stated, else `unknown` |

Fit: the existing WFS 2.0.0 connector (`srs_name`, `axis_order`,
`type_names`, `output_format`, paging) and the feature projection take a VG250
layer with configuration only, as for the Geoportal Berlin layers; geometries
are MultiPolygon in EPSG:4326 after the recorded transform. Proposed source id
`bkg-vg250-berlin`, publisher string
`Bundesamt für Kartographie und Geodäsie (VG250)`, bounded to the Berlin feature
of the Land and Gemeinde layers (filter on ARS `11*`, _verify_ filter support),
attribution as above. If the WFS lacks a GeoJSON output, the bounded download
path needs a GeoPackage decoder, which is new code (_decide in delivery_).

### Eurostat GISCO: NUTS and LAU

| Item | Record |
| --- | --- |
| Endpoint | GISCO distribution API `https://gisco-services.ec.europa.eu/distribution/v2/nuts/` with files such as `geojson/NUTS_RG_01M_2024_4326_LEVL_2.geojson` (and 2021, 2016 vintages), LAU under `.../distribution/v2/lau/` (_verify_ paths, available scales and years) |
| Access | none |
| Licence | Eurostat copyright and reuse policy, plus a GISCO-specific clause for administrative boundaries: historically "© EuroGeographics for the administrative boundaries", with use restricted to non-commercial purposes unless separately licensed (_verify_ whether this clause still applies to NUTS and LAU files after EuroGeographics' open-data changes) |
| Redistribution | with the EuroGeographics and Eurostat attribution (_verify_); restricted for commercial use if the clause stands (_verify_) |
| Vintage | the NUTS version (2016, 2021, 2024) and the LAU reference year are explicit in each file name; each is a separate vintage of the same code, and a NUTS code under two versions is two keys (as the tourism track already does) |
| Codes | `NUTS_ID`, `LEVL_CODE`, `CNTR_CODE`; LAU `GISCO_ID`, `LAU_ID` |

Fit: the GeoJSON feature decoder (`src/ingestion/geojson_features.py`) takes a
FeatureCollection with stable `NUTS_ID` identity; geometries project into
`noesis-geospatial-geometry-v2`. It would also give the tourism and labour
tracks NUTS-coded places to link to by published code. Registry: the publisher
string (`Eurostat (GISCO)`) resolves to the existing `eurostat` entry, which is
not yet a publisher for `places-geometry`, so it would raise depth.

Decision: **`not-implemented` this wave.** The licence clause for administrative
boundaries could not be verified offline, and a non-commercial restriction would
have to be carried into every evidence bundle. It is the second candidate:
once the clause is verified as open (or acceptable), GISCO NUTS for `DE3`/`DE30`
joins `geospatial.core` as an amendment.

### OpenStreetMap administrative boundaries

| Item | Record |
| --- | --- |
| Endpoint | Overpass API (`https://overpass-api.de/api/interpreter`), as `geospatial.infrastructure` already uses for `infrastructure-osm-lusatia`; or Geofabrik regional extracts (`https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf`, _verify_) |
| Access | none; Overpass usage policy limits load (_verify_) |
| Licence | Open Database License 1.0 |
| Redistribution | attribution "© OpenStreetMap contributors"; a derived database that is publicly used or shared must be offered under ODbL (share-alike), and produced works need attribution (_verify_ how the OSMF community guidelines on substantial extracts and collective databases apply) |
| Vintage | the OSM object version and timestamp per relation; no official valid time |

Fit and decision: **`not-implemented` for `geospatial.core`.** Administrative
boundaries are relations of ways; a MultiPolygon has to be assembled from member
ways by our own code, which is a construction step the store would then hold as
if published. The share-alike obligation attaches to a derived database: mixing
OSM geometries into the same store and evidence bundles as the dl-de/zero and
dl-de/by layers raises the question of which exports become ODbL derivatives,
which this audit cannot settle offline. The provenance of Berlin's boundary
relations (imported or traced from official data) is not established, so the
independence it seems to offer is doubtful. OSM stays where it is
(`geospatial.infrastructure`, with its ODbL constraints recorded in
`src/ingestion/infrastructure_sources.py`).

## Recommendation

1. **Implement BKG VG250 in `geospatial.core`** through the WFS connector
   (proposed `bkg-vg250-berlin`), bounded to the Berlin features of the Land
   and Gemeinde layers, with the 31 December vintage declared per document, the
   BKG attribution, `dl-de/by-2-0`, and precision `declared` or `unknown` as the
   product documentation states. The same place (Berlin, AGS `11000000`) is
   matched to a Land Berlin geometry only by reviewed published code, through
   option (a) above; the two geometries are never merged or snapped.
2. In the same delivery change: register `bkg` in `packs/depth.json`
   (`match`: `Bundesamt für Kartographie und Geodäsie`), add the source to
   `providers.geospatial.core.sources`, remove the `places-geometry` row from
   the thin table, and add `places-geometry` to the depth-two watchlist
   ("BKG compiles the Länder boundaries; no independent origin for the Berlin
   boundary line").
3. GISCO NUTS/LAU: `not-implemented` this wave pending the licence clause;
   OpenStreetMap: `not-implemented` for this provider.

Depth after the recommended delivery: offline 2 (Land Berlin, BKG), live 0
until the track's "Validate live coverage" run. Independent origins for the
Berlin boundary: still 1.

## Live verification

| Candidate | Status | Checked | Evidence |
| --- | --- | --- | --- |
| BKG VG250 | `unverified-live` (proposed) | - | none; publisher hosts blocked 2026-11-01 |
| Eurostat GISCO NUTS / LAU | `not-implemented` (this wave) | - | administrative-boundary licence clause not verified (_verify_) |
| OpenStreetMap boundaries | `not-implemented` (this provider) | - | ODbL share-alike on the derived database; ring assembly; provenance unclear |
