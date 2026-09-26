# Cultural collections expansion scope

Status: implemented offline inside the scientific source pack, 2026-09-26
(see `docs/guides/cultural-collections.md`). Live acceptance for DDB and
Europeana, including measured cross-provider overlap, is outstanding.

Tracking: [#1732](https://github.com/Ikey168/Noesis/issues/1732), an expansion
of the existing Science/Research and Geospatial capabilities. It does not
create a separate domain or pack. Its child
issues define scope, dependencies and acceptance.

## GitHub implementation issues

- [ ] [#1733](https://github.com/Ikey168/Noesis/issues/1733) — Select bounded Europeana and DDB collections, rights and place coverage.
- [ ] [#1734](https://github.com/Ikey168/Noesis/issues/1734) — Define cultural object, collection, creator and rights-linked evidence records.
- [ ] [#1735](https://github.com/Ikey168/Noesis/issues/1735) — Implement bounded Deutsche Digitale Bibliothek API acquisition.
- [ ] [#1736](https://github.com/Ikey168/Noesis/issues/1736) — Implement bounded Europeana API acquisition.
- [ ] [#1737](https://github.com/Ikey168/Noesis/issues/1737) — Normalize multilingual metadata, dates, places and asset references.
- [ ] [#1738](https://github.com/Ikey168/Noesis/issues/1738) — Match overlapping DDB and Europeana records without merging sources.
- [ ] [#1739](https://github.com/Ikey168/Noesis/issues/1739) — Link collection objects into Science/Research evidence workflows.
- [ ] [#1740](https://github.com/Ikey168/Noesis/issues/1740) — Project documented cultural-object place metadata into Geospatial.
- [ ] [#1741](https://github.com/Ikey168/Noesis/issues/1741) — Add rights-aware access to permitted cultural media.
- [ ] [#1742](https://github.com/Ikey168/Noesis/issues/1742) — Wire sources into existing Science and Geospatial capabilities.
- [ ] [#1743](https://github.com/Ikey168/Noesis/issues/1743) — Expose place-based primary-source discovery with scholarly context.
- [ ] [#1744](https://github.com/Ikey168/Noesis/issues/1744) — Add end-to-end offline Cultural Collections acceptance coverage.
- [ ] [#1745](https://github.com/Ikey168/Noesis/issues/1745) — Validate provider access and publish the integrated cultural-object demo.

## Use case

Discover cultural objects as primary sources for research and connect them to
places through Noesis Geospatial. Example: find archival photographs, maps, or
posters documenting a Berlin place during a specified period, see scholarly
works that explicitly cite or discuss them, and inspect the original provider
record and applicable rights.

## Initial sources

- **Deutsche Digitale Bibliothek (DDB):** public API for searching metadata and
  accessing metadata about described digital objects, including previews,
  derivatives and originals where available. Its documentation describes the
  API as open to users. [DDB API FAQ](https://pro.deutsche-digitale-bibliothek.de/faq)
- **Europeana:** aggregator API for cross-European cultural-heritage records.
  API access requires its own setup, and each object has rights metadata that
  governs reuse. [Get an API key](https://pro.europeana.eu/page/get-api),
  [rights guidance](https://pro.europeana.eu/post/europeana-and-the-fair-principles-for-research-data).

Start with selected Berlin-relevant archival photographs, maps, posters, and
records. Measure DDB/Europeana overlap and source item-level rights before
choosing fixtures or enabling asset acquisition. Europeana records can
aggregate material from DDB and other institutions; keep aggregator and
contributing-provider identities distinct.

## Expansion of existing capabilities

**Science/Research:** store cultural objects as their own source-backed record
type. Link a scholarly work to an object through an explicit provider relation,
citation, or reviewed user assertion. A shared keyword or subject is only a
discovery candidate; it does not establish that a work discusses or relies on
an object.

**Geospatial:** project only provider-supplied or reviewed locations into the
existing place store. Distinguish an institution's location from a
digitization location, depicted place, and historical event location. Retain
the source coordinates or place ID, precision, temporal evidence and any
coordinate-transform receipt. Leave ambiguous place names unresolved pending
review.

**Rights:** metadata, previews, derivatives, and originals can have different
availability and reuse conditions. Preserve item-level rights and attribution.
Default to source links when rights are missing, restricted or unclear; do not
infer that publicly searchable means redistributable.

## V1 acceptance

- Pinned native DDB and Europeana fixtures cover multilingual metadata, dates,
  collection hierarchy, rights, coordinates/place names, and duplicates.
- Repeated ingestion is idempotent; overlapping provider records retain their
  separate IDs, source versions, and rights statements.
- A research work links to a cultural object only from explicit or reviewed
  evidence; suggested matches remain marked as candidates.
- A place/time search returns objects with the right location role, source,
  precision and uncertainty; institution location is never treated as depicted
  location by default.
- Asset retrieval honors item rights; link-only status remains useful when an
  image, scan, or recording cannot be retained.
- Offline replay and bounded live checks are recorded separately for each
  provider. The demo answers a Berlin place/time primary-source question and
  exposes its source record, research links, location evidence and rights.

## Deferred

Defer unrestricted digitized-file mirroring, universal OCR/transcription,
additional archive connectors, unreviewed semantic place resolution, and a
museum-management interface. Provider record counts do not imply that the
underlying object or its media can be retained or redistributed.


There is no standalone Cultural Collections domain manifest, source-pack
manifest, enablement flag, or separate installation lifecycle in this scope.
Provider declarations extend the existing scientific/research source setup;
location links use the current GeospatialStore and query tools.
