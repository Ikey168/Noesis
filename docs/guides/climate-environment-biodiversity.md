# Climate and Environment: biodiversity occurrences and conservation status

The optional `biodiversity` feature of the Climate and Environment bundle
(`packs/climate-environment/manifest.json`, provider
`environment.biodiversity`, default **off**) takes a taxon or a place to cited
species occurrence records and to conservation status history (#2220). It adds
no pack and no server: the tools live in the knowledge-engine server beside the
other environment tools, and switching the feature off leaves the bundle, its
source packs and every other pack unchanged.

What it never does: species distribution modelling, abundance or
presence/absence estimation, locations more precise than the publisher
released, or a Noesis-derived threat status or trend.

## Sources and licences

The `climate-environment-biodiversity` source pack
(`packs/climate-environment/source_packs/`) runs through the source-pack
runtime with the `biodiversity` connector. Decisions are in the
[source audit](../development/biodiversity-evidence/source-audit.md):

| Provider | What is stored | Licence handling |
| --- | --- | --- |
| Catalogue of Life (ChecklistBank) | Name usages per named release: name, authorship, rank, status, accepted name for synonyms, classification | CC BY 4.0; every usage cites its release version and DOI |
| GBIF | Backbone species, one small occurrence page per taxon and place, dataset metadata, download DOIs | Per record: CC0, CC BY or CC BY-NC; CC BY-NC rows are flagged non-commercial; dataset citation/DOI on every row |
| IUCN Red List | Assessments at citation level (category, criteria, dates, scope, the assessor's `latest` designation, citation, URL) | Reference-only: narrative, threats, habitats, population and spatial data are never stored; exports keep citation fields only |

All three are `unverified-live` until the dated live run (BD13, #2533); the
IUCN token is the `NOESIS_IUCN_API_TOKEN` secret reference.

## Journey

1. Enable the feature through the composition coordinator (select
   `climate-environment` with feature `biodiversity`); `biodiversity_readiness`
   reports whether it is selected.
2. Run the source pack. Each CoL release is a new revision per usage, and a
   status change between releases is recorded with both releases cited. A GBIF
   record absent from a later complete page of the same selection gets a dated
   tombstone; earlier revisions stay queryable.
3. `propose_taxon_identity_matches`, then `review_taxon_identity_match`:
   cross-reference and name+authorship candidates are deterministic, name-only
   and synonym candidates lower-evidence, and split/lumped concepts are shown
   as conflicts. Nothing joins until a reviewer accepts; the acceptance records
   both checklist versions.
4. `link_biodiversity_records` links occurrences to places at their published
   precision (with geospatial `contains` receipts) or by exact country code, to
   their datasets, and datasets/assessments to papers by exact DOI.
5. Ask:
   - `lookup_taxa` - identities, matches and status changes for a name or key;
   - `occurrences_for_taxon_or_place` - records on record at a date, each citing
     occurrence, dataset, licence and retrieval time; `uncertain` records (the
     uncertainty reaches the boundary or is not published) are listed apart;
     counts are counts of records returned;
   - `conservation_status_history` - assessments per scope (global and regional
     separately) with the assessor-designated current one and published
     category changes; "not assessed on record" is not IUCN NE or DD;
   - `export_biodiversity_bundle` - the section of a place or taxon bundle.
6. `create_biodiversity_monitor` / `run_biodiversity_monitor` watch taxa, places
   or datasets for new, revised and removed occurrences, taxonomic status
   changes and new assessments, each citing the prior and new revision.

## Evidence

Offline: `tests/unit/biodiversity/` and the journey
`tests/unit/domains/test_biodiversity_acceptance.py` over pinned fixtures with
illustrative identifiers. Live: none yet; see
[`docs/development/biodiversity-evidence/README.md`](../development/biodiversity-evidence/README.md).
