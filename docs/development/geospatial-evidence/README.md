# Places and geometry evidence (geospatial.core)

Evidence for the Geospatial bundle's `geospatial.core` provider (subdomain
`places-geometry`, Geoportal Berlin WFS layers in the `geospatial-berlin`
source pack, stores `src/kb/geospatial.py` and `src/kb/geospatial_features.py`).

- [depth-audit.md](depth-audit.md) - DG01 (#2829, wave 4 tracker #2825):
  second-publisher audit under ADR-006. Recommends BKG VG250
  (`dl-de/by-2-0`, WFS) for `geospatial.core`, so Berlin gets a second geometry
  with its own vintage, matched by published code and never merged or snapped;
  Eurostat GISCO NUTS/LAU `not-implemented` this wave (administrative-boundary
  licence clause unverified) and OpenStreetMap `not-implemented` for this
  provider (ODbL share-alike, ring assembly). Records that BKG and GISCO share
  Land Berlin's origin chain. Written without network access (2026-11-01);
  terms marked _verify_, no source live.
- Offline evidence: `tests/fixtures/source_packs/geospatial-*.json`. Live
  acceptance configuration: `config/geospatial/acceptance/live-berlin.json`
  (see the [geospatial pack guide](../../guides/geospatial-pack.md)); no dated
  live run is recorded in this directory.
