# Biodiversity: source contract audit and bounded coverage (BD01, #2504)

Parent: #2220 (extends Climate and Environment, #1849). This audit records, per
source, the access method, licence and attribution, redistribution, rate
limits, authentication, sensitive-species handling and revision/as-of
semantics, and selects the bounded taxon and place coverage of the
`climate-environment-biodiversity` source pack
(`packs/climate-environment/source_packs/climate-environment-biodiversity.json`,
shipped by the Climate and Environment bundle for its optional `biodiversity`
feature). The machine-readable copy of each decision is `PROVIDER_CONTRACTS`,
`IUCN_LICENCE_DECISION`, `SENSITIVE_SPECIES_POLICY`, `BOUNDED_COVERAGE` and
`LIVE_VERIFICATION` in `src/ingestion/biodiversity_sources.py` (served by the
`biodiversity_source_contracts` MCP tool).

Nothing here was verified against a live endpoint from this runtime. Every
provider is `unverified-live`; items marked *verify* must be checked during the
live validation issue (BD13, #2533) and recorded in [`README.md`](README.md)
beside this file. Offline evidence (fixtures) is kept in the tests and never
recorded here.

## Why a separate source pack

The biodiversity sources ship as their own source pack inside the Climate and
Environment bundle (`packs/climate-environment/source_packs/`) rather than as
new entries of `climate-environment.json`: the feature is optional and off by
default, so the bundle's own `climate-environment` ^1.0.0 pin, its sources,
fixtures and hashes stay byte-identical, and only the `environment.biodiversity`
provider pins `climate-environment-biodiversity` ^1.0.0. Disabling any other
pack never depends on it.

## Scope boundary (all sources)

- No species distribution modelling, no abundance or density estimation, no
  presence/absence inference. Counts are only ever counts of records returned.
- No precise location of a sensitive species beyond what the publisher
  releases (see the sensitive-species policy below).
- No Noesis-derived threat status, trend or risk score: conservation status is
  the assessor's published category, and the current assessment is the one the
  assessor designates.
- No taxonomic opinion beyond the checklist: synonymy, rank and classification
  are stored as each checklist release publishes them.

## Per-source decisions

| Source | Access | Licence and attribution | Redistribution | Rate limits | Authentication | Revision / as-of semantics | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Catalogue of Life via ChecklistBank | HTTPS GET `https://api.checklistbank.org/dataset/{releaseKey}` (release metadata) and `/dataset/{releaseKey}/nameusage/{id}` (name usage with status, accepted name and classification; *verify* whether the classification needs the separate `/taxon/{id}/classification` call) | CC BY 4.0 for the CoL releases; cite the release with its DOI and version (*verify* per release metadata) | Permitted with attribution and the release citation | No published hard limit; the pack keeps to one request per selected usage (*verify*) | None | Monthly releases and an annual checklist, each a separate dataset key with its own version, issue date and DOI; the same CoL ID in a new release is a new revision; taxonomic status changes are dated by the later release and cite both | Selected, `unverified-live` |
| GBIF species API | HTTPS GET `https://api.gbif.org/v1/species/{taxonKey}` | GBIF Backbone Taxonomy CC BY 4.0 (*verify*); cite the backbone dataset and its version | Permitted with attribution | Undocumented soft limits on the public API; the pack keeps to single keyed requests (*verify*) | None | Backbone rebuilds change keys and statuses; each retrieval is a revision; `taxonomicStatus`, `acceptedKey` and published identifiers kept as published | Selected, `unverified-live` |
| GBIF occurrence search | HTTPS GET `https://api.gbif.org/v1/occurrence/search?taxonKey=..&country=..&geometry=..&limit=..` (one small page; `limit` <= 300 and `offset + limit` <= 100 000 are the API's own bounds) | Per publishing dataset: CC0 1.0, CC BY 4.0 or CC BY-NC 4.0 (the record's `license`); attribution to each publishing dataset | By each record's dataset licence; CC BY-NC records are flagged non-commercial in every answer | As above; one page per selection, never an enumeration | None for search | Occurrences are re-interpreted and re-published; each retrieval is a revision; a record absent from a later page of the same declared selection gets a dated tombstone revision, never a deletion | Selected, `unverified-live` |
| GBIF datasets | HTTPS GET `https://api.gbif.org/v1/dataset/{datasetKey}` | Dataset licence as published; the dataset DOI and citation text are the required attribution | As the dataset licence | As above | None | Dataset metadata (title, publisher, licence, DOI, citation, version/pub date) as a revisioned dataset record | Selected, `unverified-live` |
| GBIF occurrence downloads | HTTPS GET `https://api.gbif.org/v1/occurrence/download/{key}` (metadata of an existing download only); creating downloads (asynchronous, `POST /occurrence/download/request`) needs a GBIF account and is not automated | Every download gets a DOI which must be cited for any use of the downloaded data | As the most restrictive dataset licence in the download | Downloads are asynchronous and rate-limited per user (*verify*) | Creating: GBIF user credentials (not configured; out of scope). Reading metadata: none | A download is immutable; its DOI is recorded as a dataset record of kind `download` for citation | Metadata selected, `unverified-live`; creation `not-implemented` |
| IUCN Red List API v4 | HTTPS GET `https://api.iucnredlist.org/api/v4/taxa/sis/{sis_id}` (taxon with its assessment list) and `/api/v4/assessment/{assessment_id}` (one assessment; *verify* field names) | IUCN Red List Terms of Use: non-commercial use only; data may not be redistributed or used to build derivative datasets without IUCN permission; cite the assessment (*verify* current terms) | **Not permitted** beyond citation-level facts; see the licence decision | Token-based; IUCN asks for polite use (*verify* limits); the pack fetches one taxon and at most 10 assessments per selection | API token (registration) as secret `NOESIS_IUCN_API_TOKEN`; never in manifests, receipts or records | Assessments are published by year; every historical assessment is its own record; the assessor's `latest` flag designates the current one; regional and global assessments stay distinct by scope | Selected under **reference-only**, `unverified-live` |

## IUCN licence decision: reference-only

Decision: **reference-only** (neither "acquire in full" nor "excluded").

- Stored per assessment (citation-level facts needed to cite and date the
  assessment): assessment id, IUCN taxon (SIS) id, scientific name as
  published, Red List category code, criteria string, assessment date, year
  published, scope (global or regional, with its label), the assessor's
  `latest` designation, the possibly-extinct flags, the published reason for a
  category change (genuine / non-genuine) when stated, the citation and the
  assessment URL.
- Withheld (never stored; dropped if a response carries them and reported as
  dropped in the page receipt): rationale and narrative text, habitat and
  ecology, threats, conservation actions, use and trade, population size and
  trend, range maps and any spatial data, bibliography text, and supporting
  documentation.
- Answers and exports: every IUCN row carries the licence tier
  `reference-only` and the redistribution notice. MCP answers show the stored
  citation-level fields to the requesting user; exports (bundles) keep only
  category, year published, scope, the `latest` designation, the citation and
  the URL, so no IUCN-derived dataset beyond citation is redistributed.

## Sensitive-species policy

- Coordinates are stored only at the precision the publisher releases:
  `decimalLatitude`/`decimalLongitude` exactly as returned (already generalised
  by the publisher where it chose to), with `coordinateUncertaintyInMeters`,
  `coordinatePrecision`, `dataGeneralizations`, `informationWithheld` and the
  GBIF issue flags kept verbatim.
- A record whose coordinates are withheld stays without coordinates; nothing
  is geocoded from locality text and nothing is de-generalised.
- A generalised record carries `generalised: true`, the published text and,
  when the text states a distance or grid (for example "Coordinates generalised
  to 10 km" or "0.1 degree grid"), its parsed precision. It links only to places
  whose extent is at or above that precision; when no precision is stated it
  links only by published country code.

## Bounded coverage

| Axis | Selection |
| --- | --- |
| Taxa | *Passer domesticus* (house sparrow), *Lutra lutra* (Eurasian otter, a species whose occurrence coordinates some publishers generalise), *Corvus cornix* / *Corvus corone* (hooded and carrion crow: a concept treated as one species or two between checklists, the split/lump case) |
| Checklist releases | Two named Catalogue of Life releases (one monthly release and the following one); never "latest" |
| Places | Country `DE` (occurrence search filter and code link) and a Berlin bounding polygon (occurrence search `geometry`); linking uses geospatial places registered for those exact codes or polygons |
| Occurrence pages | One page of at most 20 records per taxon and place selection |
| IUCN | The assessment history of the selected taxa (global plus a European regional assessment where published); at most 10 assessments per taxon |
| Datasets | The publishing datasets named in the selection and one existing download's DOI metadata |

## Live verification

Every provider has a `LIVE_VERIFICATION` entry with its intended status. All
are `unverified-live` until BD13 (#2533) records a dated bounded run; GBIF
download creation is `not-implemented` (account-gated, out of scope) and the
IUCN API needs `NOESIS_IUCN_API_TOKEN` (credential not configured here).
