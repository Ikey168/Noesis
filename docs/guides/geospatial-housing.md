# Geospatial housing guide

The Geospatial bundle's optional `housing` feature (default off) takes an
address, parcel or district to a cited housing dossier. The dossier lists the
land-value zone that contains the place, with its valuation date and earlier
revisions. It lists the rent-index edition and the cells for the place's
Wohnlage, the development plans with their procedural stage, the permit and
completion statistics of the district, and the housing-indicator vintages of
the Land. Each item shows its source, unit, currency and as-of basis, with
citation links to legal works and decisions where an explicit citation exists.

Tracking: #1912. Source audit:
[geospatial-housing-source-audit](../roadmaps/geospatial-housing-source-audit.md).

## What it never does

- It gives no property valuation, rent or investment advice and no tenancy
  legal advice.
- It never interpolates, averages or merges values between zones, cells,
  reporting areas, sources or editions. **No source offers an interpolable
  value surface.** A land value applies inside its published zone for its
  Stichtag, a rent-index range to its cell for its edition, and a statistic to
  its reporting area and period.
- A point on a zone boundary, a point in no published zone and two sources
  that disagree are reported as such. The feature never picks one.

## Records (`noesis-housing-record-v1`)

`src/kb/housing.py` owns the namespace-scoped, revision-addressable
`housing_*` tables. The contract is
`contracts/schemas/jsonschema/noesis-housing-record-v1.json`.

| Record | Entity (what distinguishes two facts) | Date semantics |
| --- | --- | --- |
| land-value-zone revision | source, collection, zone number, valuation date | the published Stichtag; a missing one stays unknown |
| rent-index edition | source, edition | qualifying date and `valid_from` as the edition states them |
| rent-index cell | source, edition, published cell key | the edition's |
| development-plan stage | source, collection, plan number, stage | the published stage date; a missing one stays unknown |
| residential-area category | source, collection, edition, block id | the edition's `valid_from` |
| permit or completion statistic | source, statistic, reporting area, period, measure, vintage | period as published; the publication date is the vintage |
| housing-indicator vintage | source, series, table stamp | the GENESIS `Updated` stamp; values stay in `dataset_observations` |

Zone, plan and residential-area records reference the Geospatial feature,
feature revision and geometry they were read from. They never copy a
geometry. Each record keeps the source revision it was read from: the feature
revision and WFS page hash, or the tabular publication with its document,
page, publication URL and evidence origin.

Revisions follow one rule for every record type:

- Re-reading an unchanged entity adds nothing, whatever the fetch time or
  run.
- A changed reading of the same entity is a *correction*: a new revision that
  supersedes the current one. A return to earlier content is recorded as a
  further correction, because deduplication checks only the current
  revision.
- Different valuation dates, editions, stages, vintages or sources are
  separate entities, stored side by side. "Current" follows the source's own
  date, whatever the arrival order.
- A GENESIS table republished with other values under the same `Updated`
  stamp is refused as a silent change. A relabelled table is a correction.
- Units are labelled (through pint where installed). Money is never
  converted.

## Enabling the feature

Select the optional `housing` feature of the Geospatial bundle in the active
composition plan. It binds `geospatial.housing` together with `legal.core`,
`economics.core`, `political.core`, `news.core`, `platform.subscriptions` and
`platform.source-runtime`. The optional `housing-transit-context` feature
adds transit stops as accessibility context. There is no separate pack and no
enablement flag. `housing_readiness` reports whether the feature is selected,
how many records are stored and each provider's access decision.

## Acquisition

The housing sources ship in `geospatial-berlin` 1.3.0
(`packs/geospatial/source_packs/geospatial-berlin-1.3.0.json`), an upgrade
of the installed pack. The upgrade adds the sources below and keeps every
earlier source verbatim:

| Source | Connector | Records |
| --- | --- | --- |
| `berlin-boris-bodenrichtwerte` | `wfs` (existing WFS 2.0.0 path) | zone features, land-value-zone revisions |
| `berlin-bebauungsplaene` | `wfs` | plan features, development-plan stages |
| `berlin-wohnlagen` | `wfs` | block features, residential-area categories |
| `berlin-mietspiegel` | `housing` (`rent-index-table-csv`) | rent-index edition and cells |
| `statistik-bb-bautaetigkeit` | `housing` (`statbb-building-csv`) | permit and completion statistics |
| `destatis-genesis-bautaetigkeit` | `housing` (`destatis-genesis-ffcsv`, via `src/ingestion/connectors/dataset/genesis.py`) | `dataset_observations` plus housing-indicator vintages |

Each WFS source declares a `housing` block. The block names the published
attribute that carries the zone or plan number, the value, the valuation
date, the stage and the category. It also declares the unit, the stage
vocabulary and the Mietspiegel edition. A feature whose attributes are
missing or unreadable keeps its geometry, and the reason is recorded in
`list_housing_projection_outcomes`. A tabular document that states no
publication date and sends no `Last-Modified` header is refused.

## Dossier (`housing_dossier`)

- **Input.** One of these: a place id (an address point, or a parcel outline
  checked at its vertices), a place resolution (from
  `record_geospatial_resolution`, used once accepted in
  `review_geospatial_resolution`), a WGS84 point, or a Berlin Bezirk code.
- **Containment.** `calculate_spatial_relation` runs against the geometry
  each record references. A point-in-feature query runs against the ALKIS
  districts. Every membership has a spatial receipt.
- **As of a date.** For each source, the dossier takes the latest valuation
  date, dated stage, edition `valid_from` and vintage that is not after the
  date. Each item states its selection basis. Earlier and later revisions and
  undated readings are listed separately.
- **Rent index.** The dossier lists the cells of the valid edition whose
  published Wohnlage equals the place's source-labelled Wohnlage category.
  Dwelling age and size are not known, so no single cell is chosen.
- **Statistics.** District figures are matched by the published Bezirk code,
  which equals the ALKIS `gem` property. Land figures from Statistik BB
  (declared total row) and the Destatis indicators are listed at their own
  level. Two sources with different values for the same measure and period
  appear under `disagreements`, side by side.
- **Receipt.** Each dossier carries a receipt: a digest over every record,
  source revision and spatial receipt it used. `replay_housing_dossier` says
  `reproduced` or `changed`, and lists the records added and removed.

`compare_land_value_revisions` lists one zone's values by valuation date and
source, side by side, without computing a change.

## Citation links

`link_housing_citations` resolves each reference that a Mietspiegel edition
declares, and each reference attribute of a plan layer (the Amtsblatt notice
of an Aufstellungsbeschluss, the GVBl publication of a Festsetzung). It
matches exactly one `legal.works` work in the reference's jurisdiction:
GVBl, Amtsblatt and juris references in Berlin, BGBl in Germany. A reference
that matches nothing stays `unresolved`, with the publisher's string, the
decision identifier and the stage date. `link_housing_dossier` links a
printed-paper reference to a legislative dossier stage. A plan number or
edition name in a work title is only a discovery candidate
(`propose_housing_link_candidates`); someone other than the proposer reviews
it, and the review can be reverted. An explicit citation supersedes a pending
candidate for the same pair. News items that mention a plan or an edition
come back as discovery context (`include_news`) and are never stored as
evidence.

## Monitoring

`create_housing_monitor` makes an ordinary knowledge subscription for one
place, district, plan or zone. `run_housing_monitor` evaluates it at a
committed watermark and reports three kinds of event:

- `new_land_value_publication`, citing the zone's previous valuation date;
- `plan_stage_recorded`, citing the previous stage;
- `new_rent_index_edition`, citing the previous edition.

A correction arrives as a `changed` event. An unchanged re-acquisition emits
nothing. A missed run is recovered from the last snapshot, and replaying a
watermark adds nothing. Event text says what was published and where, and
nothing about what the change means.

## Offline and live evidence

The fixtures are authored and fictional. Tabular publications carry
`evidence_origin` (`fixture` or `live`). No provider is `verified-live`; the
bounded live validation and the published demo are #2029. The offline
journey is `tests/unit/domains/test_housing_acceptance.py`.
