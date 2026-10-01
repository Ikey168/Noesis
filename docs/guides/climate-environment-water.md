# Climate and Environment: water levels, discharge and water-body status

The `environment.water` provider of the Climate and Environment bundle
(`packs/climate-environment/manifest.json`) takes a place, a river or a
station to published water levels, discharge and Water Framework Directive
water-body status, each with its source, record revision, quality state
(provisional or approved) and as-of time (#2582). It fills the
`water-hydrology` subdomain of the domain coverage program (ADR-005). It adds
no pack and no server: three optional features, all default **off**, select it
- `water-pegelonline`, `water-usgs` and `water-eea-wise` - and the tools live in
the knowledge-engine server beside the other environment tools.

What it never does: flood forecasting, interpolation, resampling or gap
filling, its own water-body status assessment, flood-risk scoring, or a causal
link between an observation and an event. The sources carry no personal data;
personal fields are refused at write time and removed from every answer.

## Sources and licences

The `climate-environment-water` source pack
(`packs/climate-environment/source_packs/`) runs through the source-pack
runtime with the `water` connector. Decisions are in the
[source audit](../development/water-evidence/source-audit.md):

| Provider | What is stored | Licence handling |
| --- | --- | --- |
| PEGELONLINE (WSV) | Stations by UUID and number with river, location, gauge zero (value, unit, `validFrom`) and characteristic values; measurement windows of at most 31 days, timestamps and units as published | DL-DE Zero 2.0; values are unchecked raw data, stored as `provisional` with the published wording |
| USGS Water Data APIs | Monitoring locations with vertical datum and county/HUC identifiers; continuous and daily values with `approval_status` and per-value qualifiers | Public domain, USGS credited; optional key `NOESIS_USGS_WATER_API_KEY` |
| EEA WISE WFD | Ecological and chemical status per EU water-body code and reporting cycle; the reporting dataset's published water-body geometries with their vintage | CC BY 4.0 with attribution to the EEA |
| GRDC | Not implemented | Its terms forbid redistribution to third parties |

All providers are `unverified-live` until the dated live run (WA13, #2647).

## Journey

1. Select one or more water features through the composition coordinator;
   `water_readiness` reports which are selected, which stores exist and which
   linked providers (natural hazards, weather, infrastructure) are available.
2. Run the source pack. A provisional value later published as approved, a
   corrected value, a moved station or a new gauge zero is a new revision; a
   declared station or WFD row no longer served gets a dated removal. Nothing
   is overwritten or deleted.
3. `propose_water_identity_matches`, then `review_water_identity_match`:
   stations match places and rivers from published identifiers first (county
   FIPS, PEGELONLINE water shortname), then published coordinates inside a
   boundary (with a `contains` receipt and the geometry version), and river
   names only as lower-evidence candidates; water bodies reach places only
   through their published geometries. Nothing joins until a reviewer accepts;
   `revert_water_identity_match` undoes a decision. Unmatched records stay
   listed.
4. `link_water_records` links stations and water bodies to hazard records and
   documents that cite their identifiers, to weather stations whose source
   publishes the relation, to infrastructure assets that share an identifier,
   and to places by accepted match. Missing providers are reported as
   unavailable. No link claims a cause.
5. Ask:
   - `water_value_at` - the value on record at an as-of time for a station,
     parameter and time, with quality state, qualifiers, the gauge zero or
     datum, the cited revision and later revisions; a time without a published
     value stays missing;
   - `water_series` - the published values of a window with the gaps the
     station's own interval shows;
   - `water_station_history` - location and datum vintages;
   - `water_for_place` - stations and water bodies of a place or river, with
     the geometry version used and each water body's status per cycle;
   - `water_body_status_history` - WFD status per reporting cycle, never merged;
   - `export_water_bundle` - an evidence bundle in which every item cites
     source, record revision and as-of time.
6. `create_water_monitor` / `run_water_monitor` watch stations, rivers or water
   bodies for values above the station's published characteristic values (by
   default `MHW` and `HSW`), observation and station revisions, withdrawals and
   new assessment cycles; notices cite the prior and new revision and say what
   changed. They are record changes, not warnings.

## Evidence

Offline: `tests/unit/domains/test_water_*.py` and the journey
`tests/unit/domains/test_water_acceptance.py` over pinned fixtures with
illustrative identifiers. Live: none yet; see
[`docs/development/water-evidence/README.md`](../development/water-evidence/README.md).
