# Source expansions: patents, LEIs, standards and transit

These four tracks extend existing capabilities instead of adding domain packs
([#1746](https://github.com/Ikey168/Noesis/issues/1746)). Each one adds one
native source-pack connector to an existing pack, projects into an existing
store and records its provider contract. The contract says what is verified
and what is not. Live provider hosts were unreachable from the build
environment. Every live path is therefore `unverified-live`, and the fixtures
are authored rather than captured.

| Track | Pack / source | Connector | Projects into | MCP tools |
|---|---|---|---|---|
| Patents ([#1747](https://github.com/Ikey168/Noesis/issues/1747)) | `research-discovery` / `epo-ops-patents` | `epo-ops` | patent tables; Research/Technical links by review | `patent_source_contracts`, `inspect_patent_publication`, `patent_family_members`, `patent_links`, `propose_patent_link`, `review_patent_link` |
| LEIs ([#1748](https://github.com/Ikey168/Noesis/issues/1748)) | `economic` / `gleif-lei` | `gleif` | LEI entities, parent history, reporting exceptions | `company_source_contracts`, `inspect_lei_entity`, `lei_parents_as_of`, `propose_lei_registry_links`, `link_company_identity`, `review_company_identity_link` |
| Standards ([#1749](https://github.com/Ikey168/Noesis/issues/1749)) | `technical-software-knowledge` / `iso-open-data` | `iso-open-data` | Technical `standard` objects with `supersedes`/`amends` | `standards_source_contracts`, `inspect_standard_edition`, `inspect_certificate`, `import_certificates`, `propose_certificate_product_links`, `review_certificate_product_link` |
| Transit ([#1750](https://github.com/Ikey168/Noesis/issues/1750)) | `geospatial-berlin` / `vbb-gtfs` | `gtfs` | `GeospatialStore` stop points and route shapes, plus feed versions and realtime observations | `transit_source_contracts`, `transit_feed_versions`, `transit_departures`, `transit_stops_in_bbox` |

## Patents

The connector reads EPO OPS v3.2. It covers published-data bibliographic
records, INPADOC families and legal events. OPS requires OAuth2 client
credentials: a consumer key and secret stored in `NOESIS_EPO_OPS_CREDENTIALS`.
The token is requested inside the transport. Throttling responses (HTTP 403
with `X-Throttling-Control`) are reported as source limits.

Legal events are kept as source statements with their dates. Validity,
enforceability and freedom to operate are never inferred. Espacenet and WIPO
PATENTSCOPE are recorded as not implemented: Espacenet is the same data as
OPS, and PATENTSCOPE machine access is a paid subscription.

## LEIs and company identities

GLEIF records carry the following:

- the LEI (validated with the ISO 17442 check digits)
- legal name
- jurisdiction
- registration authority and registered-as number
- parent relationships with their validity periods
- reporting exceptions for each kind of parent

`lei_parents_as_of` answers ownership on a given date. OpenCorporates stays the
existing enrichment provider; it is never presented as an official register.
Links from OpenCorporates documents to LEIs are only proposed, and a person
reviews them. Official national registers are not implemented: no selected
register offers a documented free machine interface. The LEI's registration
authority and number are kept as the pointer to the official register.

## Standards and certificates

The connector reads the ISO Open Data deliverables metadata (JSONL), limited
to pinned reference prefixes and ICS codes with a record ceiling. The ISO
stage code determines the edition status (under development, published,
under review or withdrawn). Replaced editions become `supersedes` relations
and amendments become `amends` relations. Standards text is never fetched.

Certificates and conformity declarations enter only through
`import_certificates`, from a named source with locators. Each record keeps
its declared status and its status on a given date. When sources disagree,
both statuses are shown side by side. A certificate is linked to a Products
model only by an explicit brand and designation, and only after review. ETSI
and certification registries are recorded as not implemented until an
interface is validated.

## Transit

`vbb-gtfs` names one Berlin-area feed, the VBB GTFS Schedule ZIP. It pins
1–20 route ids and sets a stop-time ceiling. Parsing works as follows:

- Only the ZIP's agencies, routes, trips, stop times, stops, calendars,
  calendar exceptions, shapes and `feed_info` are read, filtered to the pinned
  routes.
- Archive members are size- and path-checked.
- The feed version is the SHA-256 of the ZIP.

Because no openly licensed GTFS Realtime endpoint was validated for VBB, the
production source declares none. A `transit.realtime_url` (HTTPS only) adds a
second page that decodes one GTFS-RT FeedMessage. A small standard-library
protobuf reader does the decoding and skips unknown fields.

Semantics:

- **Feed versions** are the schedule as published when acquired. A newer
  version replaces the current schedule. It is never evidence of what ran
  earlier. `transit_feed_versions` lists versions and route shapes, and flags
  stops whose coordinates changed between versions.
- **Service days** keep GTFS times past midnight (`25:05:00`). Times are
  resolved in the agency timezone with the GTFS rule of noon minus 12 hours,
  measured as elapsed time, so days with a daylight-saving change are
  correct. Calendar exceptions add or remove service on a date.
- **Realtime** is `observed`, `stale` (older than `realtime_max_age_s`) or
  `missing`. Cancellations, skipped stops, delays and alerts such as detours
  appear only from an observation, never from the timetable.
- **Geospatial**: stops are stored as `EPSG:4326` points and shapes as
  `LineString`s, both in the existing `GeospatialStore`. Their source records
  the feed, version and GTFS id. `transit_stops_in_bbox` answers
  stop-to-place questions for the current version.

Official rail and flight-status feeds are recorded as not implemented. Their
access, terms and temporal semantics have not been assessed.

The fixture (`tests/fixtures/source_packs/transit-gtfs.json`) is authored for a
fictional agency. To regenerate it, run
`python tests/unit/transit_fixture_builder.py`.

## What remains open

The following stay open until an operator runs them with network access and,
where needed, credentials:

- bounded live acceptance for each provider
- the OPS credentials
- confirmation of the VBB and ISO terms
- the real VBB route ids to pin (the production pack pins fixture ids, and a
  live run reports them as `missing_routes`)

Fixture coverage is not live coverage.
