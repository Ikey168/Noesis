# Natural Hazards pack

The Natural Hazards bundle (`packs/natural-hazards`, tracking issue #2207) answers one question: given a place or
bounded area and a window, which hazard events did the issuing authorities publish, what did they say at a given
date, how did they revise it, and which advisories and alerts were in force? Parameters are quoted from the issuing
body with their units and every update is kept as a revision.

It does **not** predict hazards, score risk, estimate damage or losses beyond quoting the publisher, attribute events
to climate change or any other cause, or give evacuation, safety or protective-action advice. Monitoring notices are
record changes, not warnings.

## Sources and access

| Provider | Records | Access decision |
| --- | --- | --- |
| USGS FDSN event service + PAGER | earthquakes per `updated` (magnitude type, depth, review status, network IDs; deleted and merged events kept); PAGER versions as impact estimates | unverified-live |
| EMSC FDSN event service | earthquakes per `lastupdate` with the authoring agency; never merged with USGS | unverified-live |
| GDACS event API | every episode as an event revision, an alert (level and severity text as published) and a quoted alert score; GLIDE kept | unverified-live |
| NOAA NHC | forecast/advisory and (intermediate) public advisories exactly as issued; forecast track as issued; cone by locator | unverified-live |
| Copernicus EFFIS | burnt-area polygons per `LASTUPDATE` with the published area estimate (active fires not implemented) | unverified-live |
| Copernicus GloFAS | flood notifications as the publisher's modelled output (model version, issue time, return-period thresholds) | key-gated (`NOESIS_GLOFAS_TOKEN`), optional feature `glofas`, off by default |

Contracts, licences, rate limits, revision markers and the bounded first coverage are recorded in
[the source audit](../development/hazards-evidence/source-audit.md). Every source stays `unverified-live` until a
dated live run is recorded under `docs/development/hazards-evidence/` (NH15, #2375).

## Acquisition

Sources are declared in `config/source_packs/natural-hazards.json` (connector `natural-hazards`) and run through the
source-pack runtime; each page is projected by the `noesis-hazard-record-v1` projector into `src/kb/hazards_store.py`.
Identical content read again (or read through another document) is a no-op; a publisher update is an appended
revision; an older version arriving late is history and never becomes current. Record geometry is stored by the
geospatial owner.

## Answers

- `hazard_events_for_place(namespace, start, end, place_id | point + radius_m | bbox, as_of?, basis?)` — events whose
  published geometry relates to the area in the window. Each item states the revision used (`revision_used`, on the
  publisher clock by default or the acquisition clock with `basis="acquisition"`), its full revision history with
  every parameter change, source URL and update time, and accepted correspondents from other publishers side by side.
  The answer is `events on record`, `none on record` (a covered source has no record; not a statement of safety),
  `not acquired` or `source not covered`. Windows are at most 366 days, bboxes 30°, radii 500 km.
- `hazard_alerts_in_force(namespace, at, place_id | point + radius_m, country?, as_of?)` — alerts and advisories whose
  published validity covered the place at `at`: published expiry, or supersession by the issuing body's next product
  for the same storm or GDACS event, with the chain; level, wording and watches/warnings quoted. A place matches by a
  published alert area, by watch/warning area text naming the place, or by the published affected-country list.
- `hazard_event_revisions(namespace, record_id)` — one record's revision history.
- `export_hazard_bundle(...)` — a `noesis-evidence-bundle-v1` citing every record with source, revision and as-of time.

## Correspondences, places and links

`propose_hazard_correspondences` creates candidates between publishers by shared published identifier, GLIDE or
earthquake origin proximity (60 s and 100 km, measured through geospatial relations with receipts). A different
principal accepts or rejects each (`review_hazard_correspondence`; recorded as an entity-identity `match` /
`non-match` with `merge: false`) and can revert it. Accepted correspondences only place records side by side.
`resolve_hazard_places` relates a record's published geometry to geospatial places and records the boundary vintage.
`discover_hazard_links` links records only on an explicit citation (a document quoting a published identifier), a
shared identifier or an accepted correspondence; weather warnings (#2163) and humanitarian reports (#2206) are
reported unavailable until their owners ship.

## Monitoring

`create_hazard_monitor` registers a knowledge subscription for a place, point or bbox; `run_hazard_monitor`
evaluates it at a committed watermark (no scheduler) and returns cited notices: `new_event`, `parameter_revision`,
`new_advisory`, `revised_advisory`, `new_alert`, `revised_alert`, `no_longer_in_view`, `stale_source`. Replays and
unchanged refreshes produce nothing.

## Composition

`packs/natural-hazards/manifest.json` binds `hazards.core` plus the shared `geospatial.core`, `osint.core`,
`platform.entity-identity`, `platform.subscriptions` and `platform.source-runtime` providers. Links to
climate-environment records (`environment-links`, off by default, so Climate and Environment can be disabled
independently) and weather warnings are optional features; weather warnings are omitted (reported unavailable) while
no provider declares `weather.warnings`. Offline acceptance: `tests/unit/domains/test_natural_hazards_acceptance.py`.
