# Cultural collections in Science/Research and Geospatial

Deutsche Digitale Bibliothek (DDB) and Europeana records become source-backed
primary material for research. They link to scholarly works through evidence,
and they map to places in the existing Geospatial store. There is no separate
cultural pack or domain. The two sources are part of the scientific source
pack (`config/source_packs/scientific.json`, version 1.1.0), and the Science
pack manifest lists the capability. Tracking:
[#1732](https://github.com/Ikey168/Noesis/issues/1732).

Demonstration question: *"What archival objects document this Berlin place
and period, and which research works discuss them?"*

## Sources and selection

| Source ID | Provider | Access | Scope (pinned) | Credential |
| --- | --- | --- | --- | --- |
| `ddb-berlin-photographs` | DDB | Search index `api.deutsche-digitale-bibliothek.de/2/search/index/search/select` (Solr JSON; `start`/`rows`) | `q=Berlin`, `place_fct:"Berlin"`, 2 rows per page, at most 50 records | `NOESIS_DDB_API_KEY` |
| `europeana-berlin-images` | Europeana | Search API v2 `api.europeana.eu/record/v2/search.json` (cursor paging) | `query=Berlin`, `TYPE:"IMAGE"`, 2 rows per page, at most 50 records | `NOESIS_EUROPEANA_API_KEY` |

- **Metadata terms.** Both providers publish metadata under CC0. Each digital
  object has its own item rights. The source declarations say "operator must
  confirm" until the terms are checked for the intended storage and export.
- **Field mapping.**
  - Europeana: `id`, `guid`, `dcTitleLangAware`, `year`, `rights`,
    `dataProvider`, `provider`, `edmIsShownAt`, `edmPreview`,
    `edmPlaceLatitude/Longitude/Label`, `europeanaCollectionName`.
  - DDB: `id`, `label`, `provider_fct`, `place_fct`, `time_fct`, `license`,
    `latitude`/`longitude`, `preview`, `digitalisat_url`.
  - The mapping follows the providers' documentation, and
    `cultural_source_contracts` reports it as `unverified-live`.
- **Fixtures.** `tests/fixtures/source_packs/cultural-*.json` are authored
  fictional Berlin records in those shapes, not captures. Cross-provider
  overlap and completeness can only be measured with a live run
  (`scripts/cultural_live_check.py`).

## Records

Each provider record is one object with immutable revisions. A changed
payload, such as new rights, creates a new revision, and older revisions stay
listed.

- **Identity.** Provider ID and source URL; aggregation (for example Europeana
  over DDB); contributing institution; collection hierarchy; object type.
- **Descriptions.** Titles and descriptions keep their language tags, and
  translations are never inferred. Creators and subjects stay provider
  strings.
- **Dates.** Each date keeps the original string beside a normalized year or
  range and the method used (`year-extraction-v1`). Conflicting dates stay
  side by side.
- **Places.** Each place has a role: `institution`, `digitization`,
  `depicted` or `event`.
- **Rights and representations.** The item rights statement plus separate
  representations (preview, thumbnail, original), each with its own URL.
  Asset links never enter text content.

## Rights

`cultural_rights_policy` classifies a statement:

| Category | Examples | Store asset | Export asset |
| --- | --- | --- | --- |
| public-domain | CC PDM, CC0 | yes | yes |
| open-attribution | CC BY, CC BY-SA | yes | yes |
| noncommercial | CC BY-NC*, NoC-NC | only with purpose `noncommercial-research` | no |
| restricted | InC*, CNE, UND, NKC, CC BY-ND | no | no |
| unrecognized / missing | anything else | no | no |

`acquire_cultural_assets` fetches a representation only if the rights allow
the action and the host is allowlisted. Otherwise it records `link-only`.
Expired or withdrawn URLs are recorded as `unavailable`. Metadata is never
removed when an asset is denied.

## Places (existing Geospatial store)

- Provider coordinates become points in the existing `GeospatialStore`. The
  precision is the declared value, or the pinned default (1000 m) marked
  `provider-unspecified`. Non-WGS84 coordinates (`x`/`y` plus `crs`) are
  transformed with a receipt, and out-of-range values are `coordinates-rejected`.
- Place names resolve through the existing gazetteer:
  - A single confident match is `name-resolved`.
  - Several candidates give `needs-review`, with a saved resolution.
  - No match gives `unresolved`.
- Nothing is geocoded from free text.

## Matches and research links

- **Matches.**
  - An explicit identifier match comes from a Europeana record shown at a DDB
    item URL; it is recorded as `identifier-match`.
  - Otherwise, candidates need a shared normalized title. Institution, date or
    creator conflicts make them `conflicting-candidate`.
  - Reviews are append-only and reversible, and provider records are never
    merged.
- **Research links.**
  - A link needs an `explicit_citation`, a `provider_relation` or a
    `reviewed_assertion`.
  - `suggest_cultural_research_candidates` records keyword-overlap
    candidates only.
  - `primary_sources_for_work` starts from a scholarly work (DOI or record ID).
    It returns linked objects with source links, rights and date/place
    uncertainty, plus open candidates and the geometry/place IDs.
  - Those IDs feed `search_cultural_objects(place_id=…)`, `geometry_ids`, or
    the geospatial tools.

## Queries

`search_cultural_objects` filters by place (`place_id` or `geometry_ids`),
collection, creator, subject, declared date range (by year, overlapping
normalized ranges) and provider. Undated or unplaced objects never match a
date or place filter.

## Live acceptance

```bash
python scripts/cultural_live_check.py --output docs/development/cultural-evidence/live-check-<date>.json
```

The report records counts, missing fields, the rights distribution, place
states, identifier matches, hashes, latencies and failures, never keys. The
recorded run `live-check-build-environment.json` (2026-09-26) had no keys, so
both providers were blocked at preflight with `credential_missing`.
Live acceptance and the measured cross-provider overlap remain outstanding.

## Exclusions

- Mirroring digitized files, and universal OCR/transcription.
- New archive connectors and unreviewed semantic location resolution.
- A museum-management interface.
