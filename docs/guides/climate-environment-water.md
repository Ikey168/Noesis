# Climate and Environment: water and hydrology

The optional water features of the Climate and Environment bundle
(`packs/climate-environment/manifest.json`, provider `environment.water`,
features `water-pegelonline`, `water-usgs` and `water-eea-wise`, all default
**off** and independent) take a place, a river or a station to gauging
stations, cited water levels and discharge with their quality states, and the
status history of the place's water bodies (#2582, subdomain
`water-hydrology`). They add no pack and no server: the tools live in the
knowledge-engine server beside the other environment tools, and switching the
features off leaves the bundle, its source packs and every other pack
unchanged.

What it never does: flood forecasting, interpolation, resampling or gap
filling, its own water-body status assessment (or merging reporting cycles),
flood-risk scoring, or storing personal data.

## Sources and licences

The `climate-environment-water` source pack
(`packs/climate-environment/source_packs/`) runs through the source-pack
runtime with the `water` connector. Decisions are in the
[source audit](../development/water-evidence/source-audit.md):

| Provider | What is stored | Quality and revisions | Licence |
| --- | --- | --- | --- |
| PEGELONLINE (WSV) | Stations keyed by UUID and number, the water they stand on, gauge zero with its validity, characteristic values (MW, MHW, ...), raw measurements in a bounded window | Raw data: every value `provisional`; gauge-zero and location changes are station revisions | DL-DE Zero 2.0 (*verify*) |
| USGS Water Data | Monitoring locations (HUC, datum) and daily values for declared parameters | `approval_status` Provisional/Approved and qualifiers per value; an approval is a new revision; a withdrawn value is a tombstone | U.S. public domain |
| EEA WISE WFD | Water bodies by EU code, their published geometry vintage, ecological and chemical status per reporting cycle | One record per code and cycle; cycles never merged | CC BY 4.0 (*verify*) |

GRDC is not implemented (redistribution not permitted). All sources are
`unverified-live` until the dated live run (WA13, #2647); the optional USGS key
is the `NOESIS_USGS_WATER_API_KEY` secret reference.

## Journey

1. Select one or more water features through the composition coordinator
   (`climate-environment` with `water-pegelonline`, `water-usgs` and/or
   `water-eea-wise`); `water_readiness` reports which are selected, the store
   state per provider and which linked providers are available.
2. Run the source pack. Every distinct published payload is an immutable
   revision with its retrieval time; nothing is deleted.
3. `propose_water_identity_matches`, then `review_water_identity_match`:
   published river identifiers and EU codes come before names; coordinates and
   water-body geometry are used only as published, with geospatial `contains`
   receipts naming the geometry version. Nothing joins until a reviewer
   accepts; unmatched records stay listed; `revert_water_identity_match` undoes
   a decision.
4. `link_water_records` links stations and water bodies to flood events that
   cite them, weather sources that state a gauge identifier, infrastructure
   assets sharing a published identifier, and accepted places, each with its
   basis and both revisions. Absent providers (and the registry's missing dam
   and waterway classes) are reported as `unavailable`.
5. Ask:
   - `water_for_place` - a place's (optionally one river's) stations and water
     bodies, each with its membership basis and geometry version, latest
     values with quality states and status per reporting cycle;
   - `water_value_at` - a level or discharge at a station and time as on
     record at a date, with quality state, qualifiers, the cited observation
     revision and later revisions; a time without a published value stays
     missing;
   - `water_series` - a window with missing steps listed, never filled;
   - `water_body_status_history` - status per cycle as reported;
   - `lookup_water_station` - location and gauge-zero vintages;
   - `export_water_bundle` - the place section with every item citing source,
     record revision and retrieval time.
6. `create_water_monitor` / `run_water_monitor` watch stations, rivers or water
   bodies for new values above published characteristic values (named in the
   watch; default MHW, HSW, HHW), revised or withdrawn values, station changes
   and new or corrected reporting cycles, each citing the prior and new
   revision and stating what changed.

## Evidence

Offline: `tests/unit/water/` and the journey
`tests/unit/domains/test_water_acceptance.py` over pinned fictional fixtures.
Live: none yet; see
[`docs/development/water-evidence/README.md`](../development/water-evidence/README.md).
