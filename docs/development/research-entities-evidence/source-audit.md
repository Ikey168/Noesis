# Research entities: source-contract audit, researcher data minimisation and bounded coverage (RE01)

Tracking: #2579 · delivery issue #2584 · recorded 2026-09-30.

This audit sets out, per source, what the Science bundle's optional
`research-entities-ror`, `research-entities-orcid`, `research-entities-datacite`
and `research-entities-cordis` features may acquire, how, on what terms, and how
the personal data of researchers is minimised. **It was written without network
access to the providers' documentation**: the documentation hosts
(info.orcid.org, ror.readme.io, support.datacite.org, cordis.europa.eu) were
refused by this runtime's egress proxy on 2026-09-30, so endpoints, parameters,
field names, rate limits and licence terms come from the issue's references and
the providers' published documentation as the author knows it. **Terms were not
re-verified live. Every item marked _verify_ must be checked against the live
documentation, the live terms and a real response before the first dated live
run (RE14, #2649). No provider is `live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `MINIMISATION` in
`src/ingestion/research_entities_sources.py`; the MCP tool
`research_entities_source_contracts` returns them, and each source entry in
`config/source_packs/research.json` (pack `research-discovery` 1.5.0) carries its
`research_entities.live_verification` status and the minimisation policy
`research-entities-minimisation-v1`.

These non-goals apply to every source: no researcher rankings, league tables or
metrics (h-index, citation counts, productivity scores); no inference of
affiliation from co-authorship; no author disambiguation or matching by name; no
personal data beyond the public ORCID fields allowed below; contributions are
never summed across currencies; no inferred collaboration or influence links.

## Access decisions

| Source id | Provider | Delivers | Decision (`LIVE_VERIFICATION`) | Notes |
| --- | --- | --- | --- | --- |
| `research-entities-ror` | ROR data dump (Zenodo) | organisation records per release | `unverified-live` | Bulk zip per release; Zenodo record, file and member names are _verify_ |
| `research-entities-orcid` | ORCID public API v3.0 | public researcher records | `unverified-live` | Needs registered public-API client credentials; the token is the secret `NOESIS_ORCID_PUBLIC_TOKEN`; without it the source fails `authentication_failed` and readiness reports the feature degraded |
| `research-entities-datacite` | DataCite REST API | dataset DOI metadata | `unverified-live` | No authentication for findable DOIs; `affiliation=true` is _verify_ |
| `research-entities-cordis` | CORDIS open-data exports | Horizon Europe projects and participants | `unverified-live` | Bulk zip per programme with `project.csv` and `organization.csv`; file, column and delimiter names are _verify_ |
| (none) | OpenAIRE Graph | aggregated research graph | `documented-not-acquired` | Not implemented: its organisation and author relations include inferred and deduplicated links, which the exclusions forbid, and every first-coverage record is available from the primary registries |

## Per-source contract

### ROR (`ror-dump-zip`)

* **Endpoint.** `https://zenodo.org/records/{record}/files/{release}-ror-data.zip`
  (_verify_), a zip holding the v2 schema JSON member (a list of organisations).
  The ROR API (`https://api.ror.org/v2/organizations/{id}`, used by
  `src/ingestion/ror.py`) serves only the latest release and is not used here.
* **Authentication.** None. **Key handling:** none needed.
* **Licence and redistribution.** CC0 1.0; attribution to ROR appreciated.
* **Rate limits.** One download per declared release, bounded by `max_bytes`
  (100 MB) and `max_pages` (two releases). The API limit (about 2000 requests per
  5 minutes, _verify_) does not apply.
* **Identifiers.** ROR id; external identifiers GRID, ISNI, Wikidata and FundRef
  stored as published (`type`, `all`, `preferred`).
* **Updates, corrections and removals.** Each dump release (`vN.N`, dated) is a
  vintage. A record changed between releases becomes a new revision of its
  organisation record; `admin.last_modified` states its own change date. Records
  are not deleted by ROR: a merged or closed organisation stays with status
  `withdrawn` or `inactive` and a `successor` (or `predecessor`) relationship. A
  declared id missing from a release is reported in the receipt as
  `not_in_response` and never recorded as a deletion.

### ORCID (`orcid-record-json`)

* **Endpoint.** `GET https://pub.orcid.org/v3.0/{orcid}/record` with
  `Accept: application/vnd.orcid+json` (_verify_).
* **Authentication and key handling.** Registered public-API client credentials;
  the operator obtains a `/read-public` token (`https://orcid.org/oauth/token`,
  client-credentials grant) and supplies it as `NOESIS_ORCID_PUBLIC_TOKEN`. The
  token is sent as `Authorization: Bearer` only to the declared host and never
  appears in a manifest, record, receipt or log.
* **Licence and redistribution.** ORCID releases public data under CC0
  (_verify_); the ORCID Public API client terms apply to the client (_verify_ the
  wording on presenting ORCID data).
* **Rate limits.** About 24 requests per second with a burst of 40 per client
  (_verify_); one request per declared iD.
* **Identifiers.** ORCID iD with its ISO 7064 11,2 checksum (validated before
  any request); works carry external ids (DOI etc.) as asserted; employments carry
  disambiguated organisation ids (ROR, GRID, Ringgold, FundRef) as asserted.
* **Updates, corrections and removals.** `history.last-modified-date` dates a
  record version; a changed public record is a new revision. An item a
  researcher makes private or deletes disappears from the next response and the
  revision without it is stored. A deactivated or deprecated record (HTTP 409 or
  410, _verify_) becomes a withdrawn revision carrying no personal field.

### DataCite (`datacite-doi-json`)

* **Endpoint.** `GET https://api.datacite.org/dois/{doi}?affiliation=true&publisher=true`
  (JSON:API, _verify_).
* **Authentication.** None for findable DOIs. **Key handling:** none needed.
* **Licence and redistribution.** DataCite metadata is CC0 (_verify_).
* **Rate limits.** About 3000 requests per 5 minutes per client IP (_verify_);
  one request per declared DOI.
* **Identifiers.** DOI (stored lower-case); related identifiers with
  `relatedIdentifierType` and `relationType` (IsSupplementTo, Cites, IsVersionOf,
  ...) exactly as published; funding references with funder identifier and award
  number.
* **Updates, corrections and removals.** `metadataVersion` and `updated` identify
  a metadata version; a changed response is a new revision. A DOI no longer
  served (HTTP 404, for example after it was made registered-only) becomes an
  `unavailable` revision, never a deletion.

### CORDIS (`cordis-projects-csv-zip`)

* **Endpoint.** `https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip`
  and `.../cordis-h2020projects-csv.zip` (_verify_): a zip with semicolon
  delimited `project.csv` and `organization.csv`.
* **Authentication.** None. **Key handling:** none needed.
* **Licence and redistribution.** Commission reuse policy (Decision
  2011/833/EU), CC BY 4.0 with attribution to CORDIS (_verify_).
* **Rate limits.** Not documented; one download per declared programme per run,
  bounded by `max_bytes` (100 MB).
* **Identifiers.** Project id (the grant agreement number) within its framework
  programme; participant `organisationID` (the PIC, _verify_) and VAT number as
  published; topic and call identifiers.
* **Contributions.** `ecContribution`, `netEcContribution` and `totalCost` are
  stored as published (text and exact decimal; a decimal comma is read, _verify_)
  with the currency EUR, stated because the export carries no currency column.
* **Updates, corrections and removals.** `contentUpdateDate` of the project and
  its participant rows dates a project version; a corrected row set is a new
  revision. A declared project missing from a later export is reported as
  `not_in_response` and never recorded as a deletion.

## Researcher data-minimisation decision (binding for every research-entities module)

Researchers are natural persons. ORCID makes a record public because its holder
chose to, not so that researchers can be profiled or ranked. The decision
follows the rule "store only what the research questions need, and only what the
holder made public":

1. **Who is a natural person.** ORCID record holders; creators and contributors
   of `nameType` `Personal` in DataCite metadata. CORDIS participants are
   organisations.
2. **Stored for a researcher.** The ORCID iD; the name (given names, family
   name, credit name) only when its visibility is `PUBLIC`; the record's
   last-modified time; public employments (organisation name, city, region and
   country, disambiguated organisation id, department, role title, start and end
   dates, put-code, whether the researcher or a member client asserted it, and
   the member client's name and id - a member client is an organisation); public
   works (put-code, type, title, publication year, external ids as asserted,
   asserting source kind).
3. **Never stored.** Biography, emails, addresses (country of residence),
   keywords, other names, researcher URLs, person external identifiers,
   educations and qualifications, distinctions, invited positions, memberships,
   services, fundings, peer reviews, research resources, any item that is not
   public, and the name of a self-asserting source. The parser drops these before
   any record or receipt is written and lists the dropped sections under
   `withheld_sections`; the record store refuses (`minimisation_violation`) a
   researcher record with any other field, so a faulty adapter cannot write them.
4. **Dataset persons.** A personal creator or contributor keeps its name type,
   its ORCID iD when the metadata publishes one, its affiliation identifiers
   (ROR) and its position; given, family and full names, person affiliation
   names and other name identifiers are never stored.
5. **Never matched.** Researchers are never proposed as identity candidates,
   merged or disambiguated by name. A researcher links to papers only through
   DOIs asserted in their public ORCID record and to organisations only through
   the disambiguated organisation id of an asserted employment; these links are
   labelled ORCID-asserted, never verified authorship. Dataset persons are never
   matched.
6. **Who may query them.** Researcher records are returned only to principals
   holding `knowledge:science:research-entities:researchers:read` in addition to
   the read scope; other principals see a count of withheld researcher records.
7. **Retention.** Revisions are retained with the minimised fields. A
   deactivated or deprecated record becomes a withdrawn revision without a name.
   On a holder's request, an operator redaction
   (`ResearchEntityStore.redact_researcher`) removes the name from every stored
   revision of that researcher and records the redaction (who, when, why). No
   automatic expiry is implemented in the first coverage.
8. **Notices and exports.** Monitor notices and evidence bundles carry the ORCID
   iD and the minimised fields only.

## Bounded first coverage

* **ROR:** the two most recent dump releases, restricted to a declared list of at
  most 200 ROR ids: the organisations of the declared CORDIS participants and
  ORCID employments, their parents, children and successors. Enough to answer
  lineage across two releases without mirroring the registry.
* **ORCID:** a declared list of at most 50 ORCID iDs whose public records name a
  declared ROR organisation; never expanded through co-authorship.
* **DataCite:** a declared list of at most 50 dataset DOIs per source whose
  metadata cites a declared organisation's ROR id or a declared CORDIS project.
* **CORDIS:** the Horizon Europe (and, when declared, Horizon 2020) export,
  restricted to a declared list of at most 200 project ids per programme.
* **Periods:** records as published in the acquired releases; no backfill.
* Until the RE14 live run the selections name fictional placeholders (Exampla,
  Northwind); nothing implies complete coverage of any registry.

## LIVE_VERIFICATION

Every provider is `unverified-live`: no dated live run exists from this
runtime. Offline fixture evidence lives in `tests/unit/domains/test_research_entities_*.py`
and is never recorded as live evidence.
