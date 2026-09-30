# Research entities: source audit, data-minimisation decision and bounded coverage (RE01)

Tracking: #2579 · delivery issue #2584 · recorded 2026-09-30.

This audit sets out, per source, what the Science bundle's optional
`research-entities-ror`, `research-entities-orcid`, `research-entities-datacite`
and `research-entities-cordis` features may acquire, how, and on what terms.
The official pages were read on 2026-09-30, but this runtime's egress proxy
blocked `info.orcid.org`, `ror.readme.io`, `support.datacite.org`,
`cordis.europa.eu`, `data.europa.eu` and `zenodo.org`. Everything below that
comes from those pages was taken from web-search summaries of the cited URLs,
not from the pages themselves. **Every point marked _verify_ is unverified and
must be checked against the live documentation, the live terms and a real
response before the first dated live run (RE14, #2649). No provider is `live`
until that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `LIVE_VERIFICATION`, `BOUNDED_COVERAGE`, `MINIMISATION`
and `NOT_IMPLEMENTED` in `src/ingestion/research_entities_sources.py`. The MCP
tool `research_entities_source_contracts` returns them, and each source entry in
`config/source_packs/research.json` (pack `research-discovery` 1.5.0) carries
its `research_entities.live_verification` status. Coverage is added to the
existing Science pack. No new pack is created.

These non-goals apply to every source. There are no researcher rankings or
metrics: no h-index, no citation or usage counts, no impact scores. DataCite
`citationCount`, `viewCount` and `downloadCount` are dropped, and the record
contract refuses such keys. Affiliation is never inferred from co-authorship.
Authors are never disambiguated or matched by name. No personal data is kept
beyond the public ORCID fields that the minimisation decision below allows.
Researchers are never merged. Contributions are never summed across currencies
or converted. No collaboration or influence link is derived.

## Access decisions

| Source | Delivers | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- |
| ROR data dump (zenodo.org) | organisations by ROR ID per data-dump release | `unverified-live` | The dump is published on Zenodo under CC0 with no authentication. It holds every status (active, inactive, withdrawn) since December 2022. The release file and JSON member names, and whether the zip fits the 100 MB byte ceiling, are _verify_. |
| ORCID Public API (pub.orcid.org, v3.0) | public records of declared ORCID iDs | `unverified-live` | The API serves public data. Its terms grant a limited licence for **non-commercial use** only: no re-use fees and no revenue-generating product. An operator must confirm non-commercial use before enabling the source. Commercial deployments use the CC0 annual public data file instead, which is not implemented here. Path, media type, deactivation and lock responses are _verify_. |
| DataCite REST API (api.datacite.org) | DOI metadata and related identifiers | `unverified-live` | Retrieval needs no authentication and the metadata is CC0. The `affiliation` and `publisher` parameters and the query syntax for related identifiers are _verify_. |
| CORDIS bulk files (cordis.europa.eu) | framework-programme projects with participants | `unverified-live` | EU-owned CORDIS content is reusable under CC BY 4.0 (Commission Decision 2011/833/EU). File names, delimiter, decimal separator and the file-level licence on data.europa.eu are _verify_. |

## Not implemented

| Candidate | Reason |
| --- | --- |
| OpenAIRE Graph | A gap-table candidate outside the tracker's initial sources. Its graph deduplicates organisations and derives inferred author and affiliation relations, which the exclusions forbid unless they are filtered out. It needs its own audit and was not audited here. |
| ORCID Member API | It reads limited-visibility data. The minimisation decision allows public fields only. |

## Per-source contract

| Source | Endpoint, format | Authentication and keys | Licence | Rate limits and bounds |
| --- | --- | --- | --- | --- |
| ROR | `https://zenodo.org/records/{record}/files/{file}`: a zip holding the schema v2 JSON array (the CSV copy is not read; _verify_) | none | CC0 1.0 | Zenodo download limits are not documented per file (_verify_). One download per declared release. `max_bytes` 100 MB; at most 200 declared ROR IDs per release. |
| ORCID | `GET https://pub.orcid.org/v3.0/{iD}/record`, JSON (_verify_) | Optional `/read-public` bearer token (client credentials), referenced as `NOESIS_ORCID_READ_PUBLIC_TOKEN` and resolved at run time. It is never stored in manifests, receipts or records. | ORCID Public API terms: non-commercial licence. The annual public data file is CC0 (_verify_ the current terms text). | 12 requests/second for the Public and Anonymous APIs from February 2025 (previously 24), burst 40, and a quota of 100 000 reads a day per client (_verify_). One request per declared iD, at most 50 iDs. |
| DataCite | `GET https://api.datacite.org/dois/{doi}?affiliation=true&publisher=true`, or a bounded query page `dois?query=…&page[size]≤100`, JSON:API (_verify_) | none (identified requests get a higher tier) | CC0 1.0 for the metadata; the waiver does not cover the described datasets | 3000 / 1000 / 500 requests per 5 minutes per IP for authenticated / identified / unidentified requests (_verify_). One request per declared document, at most 100 DOIs. |
| CORDIS | `https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip` (also `cordis-h2020projects-csv.zip`): `project.csv` and `organization.csv`, semicolon-separated, comma decimals (_verify_) | none | CC BY 4.0 with attribution to CORDIS (_verify_) | Not documented. One download per declared file, at most 200 declared project IDs. |

## Identifiers, revisions, corrections and removals

| Source | Record key | What marks a revision | Corrections and removals |
| --- | --- | --- | --- |
| ROR | ROR ID; GRID, ISNI, Wikidata and FundRef kept as published | Each release is a vintage. Its version and declared date mark the revision, and `admin.last_modified` is kept. A record whose content changed between releases gains a revision; an unchanged record is recorded as a member of the release. | ROR never deletes records. It makes them `inactive` or `withdrawn` and states `successor`/`predecessor` relationships. A declared ID missing from a release is a `not_in_release` revision. |
| ORCID | ORCID iD (ISO 7064 11,2 check digit) | `history.last-modified-date`. A change to a field outside the minimisation decision is not a revision. | An item made private disappears in a new revision. A deactivated record (deactivation date), a locked record (HTTP 409) or an unknown iD (HTTP 404) becomes a removal revision without personal fields (_verify_ the response codes). |
| DataCite | DOI | `metadataVersion` and `updated` | A metadata update raises `metadataVersion`. A DOI that is no longer findable (HTTP 404) becomes a `not_found` revision. |
| CORDIS | frameworkProgramme and project `id`; participants by PIC (`organisationID`) with VAT number | `contentUpdateDate`, within the declared file release | A corrected row has a later `contentUpdateDate`. A declared project missing from a later file is `not_in_release`. |

## Data-minimisation decision

Personal data appears in ORCID records and DataCite creator lists. CORDIS
participants are organisations. The decision below is enforced at write time
by `src/kb/research_entities_records.py` (`minimisation_violations`), which
refuses any statement that carries more.

- **Stored from ORCID**, public items only:
  - the iD;
  - the public display name (credit name, else given and family names);
  - public employments: organisation name, city, country, the disambiguated organisation identifier (ROR, GRID, Ringgold or FundRef), start and end;
  - public works: put-code, title, type, publication year and self identifiers of type DOI, arXiv or PMID;
  - item and record last-modified times.
- **Excluded from ORCID**:
  - emails, addresses, biography, keywords, other names, researcher URLs and person external identifiers;
  - educations, qualifications, invited positions, distinctions, memberships, services, fundings, peer reviews and research resources;
  - employment department and role title;
  - work contributors, citations, journal titles and URLs;
  - any item whose visibility is not `public`.
- **Stored from DataCite creators**: position, name type, the ORCID iD, an organisational creator's name, and affiliation identifiers. Personal names, given and family names, the affiliation names of personal creators, contributors and usage metrics are excluded.
- **Dropped from CORDIS**: street, postcode, geolocation, contact form and objective texts. The organisation website is kept as identity evidence.
- **Retention**: revisions are immutable and kept, because as-of answers need them. Once ORCID reports a record deactivated, locked or unknown, every answer withholds the display name of every earlier revision. Monitors name changed researcher fields without repeating their values.
- **Who may query**: researcher records and researcher monitors need `knowledge:research-entities:researchers` in addition to `knowledge:research-entities:read`. Without it, answers withhold the ORCID iDs of DataCite creators.
- **Merges**: researchers are never subjects of identity matching or entity merges. They reach papers only through the works they assert in ORCID.

## Bounded first coverage

| Source | Selected coverage | Justification |
| --- | --- | --- |
| ROR | Up to 200 declared ROR IDs (the journey's organisations and their declared relatives) from the two most recent releases | Covers lineage across one merger with release vintages, without mirroring the registry |
| ORCID | Up to 50 declared iDs, each with a recorded reason. No search and no co-author crawling. | Personal data stays limited to researchers an operator names on purpose |
| DataCite | Up to 100 declared DOIs, or query pages of at most 100 datasets for a declared client or related paper DOI | Reaches the datasets of a paper or an organisation without a bulk harvest |
| CORDIS | Up to 200 declared project IDs per file, HORIZON and H2020, from the two most recent files | Covers participants and contributions of the journey's projects |

## Sources read (2026-09-30)

| URL | Outcome |
| --- | --- |
| https://info.orcid.org/documentation/features/public-api/ | not fetched (egress blocked) |
| https://info.orcid.org/refining-api-traffic-management/ | web-search summary: rate limits and quotas above (_verify_) |
| https://info.orcid.org/terms-of-use/ | web-search summary: Public API non-commercial licence; public data file CC0 (_verify_) |
| https://ror.readme.io/docs/data-dump | not fetched (egress blocked); web-search summary: CC0, all statuses in the dump (_verify_) |
| https://ror.readme.io/docs/zenodo | not fetched; concept DOI 10.5281/zenodo.6347574 from the search summary (_verify_) |
| https://support.datacite.org/docs/api | not fetched (egress blocked) |
| https://support.datacite.org/docs/rate-limit | web-search summary: tiers above (_verify_) |
| https://support.datacite.org/docs/datacite-metadata-license | web-search summary: CC0 1.0 metadata waiver (_verify_) |
| https://cordis.europa.eu/about/services | not fetched (egress blocked) |
| https://cordis.europa.eu/about/legal | web-search summary: CC BY 4.0 under Decision 2011/833/EU (_verify_) |
| https://data.europa.eu/data/datasets/cordis-eu-research-projects-under-horizon-europe-2021-2027 | not fetched (egress blocked); search summary: CSV, JSON, XML and XLS distributions (_verify_) |
| https://data.europa.eu/data/datasets/cordish2020projects | search summary: `organization.csv` columns listed above (_verify_) |
