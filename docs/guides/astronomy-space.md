# Astronomy and Space guide

The Astronomy and Space bundle (`packs/astronomy/`, tracking issue #2149)
answers what the responsible bodies published about an astronomical object,
satellite, launch or date, and when. Every answer quotes its publisher with
the source, revision and as-of time. Noesis determines no orbit, computes no
ephemeris, close approach or conjunction, issues no impact-risk verdict,
dispositions no exoplanet and gives no operational space-weather advice.

## What it covers

| Provider | Sources | Answers |
| --- | --- | --- |
| `astronomy.small-bodies` (always on) | MPC designation identifier API, MPCORB, JPL SBDB, JPL Sentry (quoted) | `small_body_history`, `orbit_solution_as_of`, `impact_risk_listing_as_of`; also the record owner, identity, citations and monitors |
| `astronomy.exoplanets` (always on) | NASA Exoplanet Archive TAP (`ps`, `pscomppars`, `toi`, `cumulative`) and its removed-planets listing | `exoplanet_status_as_of` |
| `astronomy.launches` (optional `astronomy-launches`, off by default) | GCAT launch log, satellite catalogue and organisations; CelesTrak SATCAT | `lookup_launches`, `orbital_object_history`, `review_astronomy_launch_site` |
| `astronomy.space-weather` (optional `astronomy-space-weather`, off by default) | NOAA SWPC `alerts.json` | `space_weather_alerts` |

The composition feature ids use hyphens because composition feature ids
cannot contain underscores. Sources, licences, attribution, bounds and the
access decisions for Horizons, Space-Track and ESA NEOCC are in the
[source audit](../development/astronomy-evidence/source-audit.md).

## Acquire

The `astronomy-and-space` source pack (`config/source_packs/astronomy.json`)
runs through the shared source-pack tools. Each source is bounded by declared
objects, hosts, launch years or organisation codes; rows outside the bounds
are counted in the page receipt. Nothing is live-verified yet: every source is
`unverified-live` until a dated live run (AS13).

## Ask

Every answer states `knowledge_cutoff` (published by the end of the `as_of`
day, UTC, and optionally acquired by `acquired_by_ms`), an integer `n` and a
`status`:

* `answered`: published records exist at the cutoff;
* `not_yet_published`: the object is on record but first published later;
* `unknown`: nothing on record (not acquired, or outside the bounds).

```text
small_body_history(namespace="astronomy", designation="2099 AB12", as_of="2099-04-30")
orbit_solution_as_of(namespace="astronomy", designation="2099 AB12", as_of="2099-05-10", publisher="JPL")
exoplanet_status_as_of(namespace="astronomy", planet="TOI-99902.01", as_of="2099-03-01")
lookup_launches(namespace="astronomy", provider="FICTSPACE")
space_weather_alerts(namespace="astronomy", window_from="2099-09-01", window_to="2099-09-02")
```

Orbit solutions are listed per publisher and solution ID with epoch (exact
Julian-date text), arc and observation count; values from different solutions
are never averaged. Dispositions are per archive table, with the native code,
the reference and any later change shown as later. GCAT and SATCAT
disagreements (for example decay dates) are shown side by side.

## Identity and citations

* Designations link only through MPC identifications and designations a
  source states; packed and unpacked forms normalise deterministically.
* Exoplanet cross-identifiers link when the archive states them (a planet
  row's `toi`, a KOI's `kepler_name`); a TOI that only shares a host
  identifier is a reviewable candidate.
* COSPAR/NORAD pairs link GCAT and SATCAT when both state them alike; a
  disagreement is a review candidate.
* Launch providers match `canonical_entities` organisations only through a
  reviewed candidate (an entity identity decision); similar names are never
  accepted. Launch sites get a saved Geospatial resolution that the
  Geospatial review accepts.
* `link_astronomy_citations` resolves stated bibcodes and DOIs to Science
  paper records by exact identifier. Unresolved references, including MPC
  circulars, stay visible.

## Monitor

`create_astronomy_monitor` watches a small body, exoplanet, host, orbital
object, launch provider or site, or SWPC products by type and NOAA scale.
Monitors are knowledge subscriptions evaluated at committed watermarks; there
is no separate scheduler. Events cite the old and the new revision.

## Evidence

The offline journey is
`tests/unit/domains/test_astronomy_acceptance.py`; it runs with sockets
blocked over authored fixtures naming fictional objects. Bounded live
evidence (AS13) will be recorded under `docs/development/astronomy-evidence/`.
