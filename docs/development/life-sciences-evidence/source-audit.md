# Life sciences: source contract audit and bounded coverage (LS01, #2656)

Parent: #2652 (wave 1 of the domain coverage program, #2578; subdomain
`life-sciences-reference`). This audit records, per source, the endpoints,
authentication and key handling, licence and redistribution terms, rate
limits, and how updates, corrections and removals are identified; the
data-minimisation decision; and the bounded first coverage of the four
`life-sciences` sources in `config/source_packs/scientific.json`
(`primary-scientific-evidence` 1.2.0). The machine-readable copy of each
decision is `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `LIVE_VERIFICATION` and
`NOT_IMPLEMENTED` in `src/ingestion/lifesci_sources.py` and `MINIMISATION` in
`src/kb/lifesci_records.py` (served by the `lifesci_source_contracts` MCP tool).

Nothing here was verified against a live endpoint. Every source is
`unverified-live`; items marked *verify* must be checked by the live validation
issue (LS14, #2721) and recorded in [`README.md`](README.md). Offline evidence
(synthetic fixtures) is kept in the tests and never recorded here.

## How the terms were read (2026-09-30)

The audit environment's egress proxy blocked every official page named below
(`www.uniprot.org`, `www.ncbi.nlm.nih.gov`, `www.rcsb.org`,
`chembl.gitbook.io`); none could be fetched directly. Terms were read from
search-result extracts of the official pages returned by a web search on
2026-09-30. Each point below says which. A point with no extract is marked
**unverified** and must be confirmed from the page itself before the live run.

| Page | URL | Read on 2026-09-30 |
| --- | --- | --- |
| UniProt licence and disclaimer | https://www.uniprot.org/help/license | search-result extract only (page blocked) |
| UniProt REST API documentation | https://www.uniprot.org/help/api, https://rest.uniprot.org/ | not fetched (blocked); endpoint shapes unverified |
| UniProt "link to old versions" (UniSave) | https://www.uniprot.org/help/link_old_versions | search-result extract only |
| NCBI Datasets v2 API and API keys | https://www.ncbi.nlm.nih.gov/datasets/docs/v2/api/, https://www.ncbi.nlm.nih.gov/datasets/docs/v2/api/api-keys/ | search-result extract only |
| NCBI Website and Data Usage Policies | https://www.ncbi.nlm.nih.gov/home/about/policies/ | search-result extract only |
| RCSB PDB Usage Policies | https://www.rcsb.org/pages/usage-policy | search-result extract only |
| RCSB PDB Data API | https://data.rcsb.org/ | not fetched (blocked); endpoint shapes unverified |
| ChEMBL interface documentation (About, licence) | https://chembl.gitbook.io/chembl-interface-documentation/about | search-result extract only |
| ChEMBL web services | https://www.ebi.ac.uk/chembl/api/data/docs | not fetched (blocked); endpoint shapes unverified |

## Scope boundary (all sources)

- No biological or clinical inference: no function, interaction, pathway,
  disease or drug-target claim is derived; a link records an identifier, an
  accepted match or a citation, never a relationship Noesis inferred.
- No activity prediction, no binding, toxicity or druggability score.
- No sequence analysis beyond storage: sequences are stored as published
  (value, length, mass, checksums) and never aligned, compared or annotated.
- Activity values are kept as the published strings (ChEMBL's published and
  standardised type, relation, value and unit side by side) and are never
  converted, compared or aggregated across assays or assay types. pChEMBL and
  computed molecule properties are not stored.
- No redistribution beyond each source's licence (below); no bulk mirrors.
- Computed structure models (AlphaFold, ModelArchive) are out of scope; only
  experimental PDB entries are acquired.

## Data minimisation (personal data)

The reference records are not about people, but four sources carry person
names beside them:

| Source | Personal data present | Decision |
| --- | --- | --- |
| UniProt | reference author lists; submission names | dropped at acquisition |
| RCSB PDB | `audit_author`, citation authors (`rcsb_authors`) | dropped at acquisition |
| ChEMBL | document `authors` | dropped at acquisition |
| NCBI Gene and Taxonomy | none in the selected fields (nomenclature authorities are organisations; taxonomic authority strings such as "Fictor 2090" are nomenclature, kept as published) | nothing to drop |

- **Stored:** literature is cited by DOI, PubMed ID, title, journal and year
  only.
- **Excluded:** every author, depositor, submitter, curator, contact, e-mail
  and ORCID field (`PERSONAL_KEYS`). Adapters drop them and list them in each
  page receipt (`personal_fields_dropped`); `statement()` rejects them with
  `personal_field` at write time; the MCP tools refuse to return an answer that
  carries one. Nothing is redacted in place.
- **Retention:** records are immutable revisions of reference data; since no
  personal data is stored, no personal-data retention period applies.
- **Who may query:** every principal with `knowledge:lifesci:read` and
  namespace access; there is nothing personal to restrict further.

## UniProt (`uniprot-proteins`, provider `uniprot`)

- **Endpoints:** `GET https://rest.uniprot.org/uniprotkb/{accession}?format=json`
  (entry) and `GET https://rest.uniprot.org/unisave/{accession}?format=json`
  (entry-version history). UniSave is confirmed by the extract of "How do I
  link to a specific version of a UniProtKB entry?" and by indexed
  `rest.uniprot.org/unisave/{accession}?format=txt&versions=N` URLs; the JSON
  field names (`results`, `entryVersion`, `sequenceVersion`, `firstRelease`,
  `lastRelease`) are *verify*. The release header `X-UniProt-Release` and
  `X-UniProt-Release-Date` are *verify*.
- **Authentication:** none; open access with no login (third-party summary of
  the API paper; the official API page could not be read — unverified).
- **Licence:** CC BY 4.0 (extract of https://www.uniprot.org/help/license:
  "UniProt content is distributed under the Creative Commons Attribution (CC BY
  4.0) License"). **Redistribution:** permitted with attribution to the UniProt
  Consortium, citing accession, entry version and release.
- **Rate limits:** no official limit could be read (**unverified**); a
  third-party summary states no hard published limit. The selection is at most
  20 accessions per run, one request each plus one history request.
- **Updates, corrections, removals:** entry version and sequence version
  (UniProt's own counters) per entry; each UniProt release read is a revision.
  An entry that leaves UniProtKB is returned as `entryType: "Inactive"` with
  `inactiveReason.inactiveReasonType` MERGED, DEMERGED or DELETED and
  `mergeDemergeTo` successors (*verify*); it is stored as an `obsoleted`
  revision naming its successors, and queries resolve it to them. Secondary
  accessions listed by an entry resolve to that entry.
- **Reviewed/unreviewed:** labelled as UniProt labels them (`entryType`
  "UniProtKB reviewed (Swiss-Prot)" / "UniProtKB unreviewed (TrEMBL)").
- **Not stored:** comments, features and keywords (annotation text is out of
  the bounded scope; reported as `excluded_fields_dropped`).

## NCBI Gene and Taxonomy (`ncbi-genes-taxonomy`, provider `ncbi`)

- **Endpoints:** `GET https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/{gene_id}`
  and `GET https://api.ncbi.nlm.nih.gov/datasets/v2/taxonomy/taxon/{tax_id}`
  (base URL confirmed by the API-keys page extract; report field names
  `reports[].gene`, `swiss_prot_accessions`, `taxonomy.classification`,
  `parents` are *verify*).
- **Authentication and key handling:** optional NCBI API key, held as the
  `NOESIS_NCBI_API_KEY` secret reference and sent only in the `api-key` request
  header (extract of the API-keys page: "pass your API key as a header ... the
  `api-key` header"). It never appears in a manifest, URL, receipt or record;
  a response echoing it is refused.
- **Rate limits:** 5 requests per second without a key, 10 with a key (extract
  of the Datasets API-keys page). Selections stay far below: four requests per
  run.
- **Licence:** "NCBI places no restrictions on the use or distribution of the
  data contained in molecular databases"; submitters may claim rights in
  portions and NCBI cannot grant unrestricted permission (extract of the NCBI
  Website and Data Usage Policies). **Redistribution:** the stored identity,
  status and lineage fields are redistributed with the Gene ID or Tax ID; the
  operator confirms before bulk redistribution.
- **Updates, corrections, removals:** the reports carry no release label, so
  every change of published content is a revision dated by retrieval.
  Replaced and discontinued Gene IDs and merged Tax IDs are `obsoleted`
  revisions naming the current ID; how Datasets reports them (a gene warning
  with `replaced_id`, a taxonomy report answering a merged query with the
  current node) is *verify*.

## RCSB PDB (`rcsb-pdb-structures`, provider `pdb`)

- **Endpoints:** `GET https://data.rcsb.org/rest/v1/core/entry/{pdb_id}`,
  `GET https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}`
  (at most 10 per entry) and `GET https://data.rcsb.org/rest/v1/holdings/removed/{pdb_id}`
  for obsolete entries (paths and field names *verify*).
- **Authentication:** none.
- **Licence:** "data files contained in the PDB archive are available under
  the CC0 1.0 Universal (CC0 1.0) Public Domain Dedication. All data provided
  by RCSB PDB programmatic APIs are available under the same license"; users
  are encouraged to attribute the original authors (extract of the RCSB PDB
  Usage Policies). **Redistribution:** permitted; attribution is given by the
  PDB ID and primary citation (DOI, PubMed ID), not by storing author names.
- **Rate limits:** none could be read (**unverified**); at most 11 requests per
  entry, three entries per run.
- **Updates, corrections, removals:** the entry's major.minor revision and
  `pdbx_audit_revision_history` are stored as published; each PDB revision is a
  record revision. An obsolete entry is an `obsoleted` revision from the
  removed holdings with its removal date and `id_codes_replaced_by`; the
  superseding entry keeps `pdbx_database_PDB_obs_spr` (SPRSDE).
- **Stored as published:** experimental methods, `resolution_combined`, and
  each polymer entity's UniProt accessions as the PDB states them (entity
  container identifiers).

## ChEMBL (`chembl-bioactivity`, provider `chembl`)

- **Endpoints:** `GET https://www.ebi.ac.uk/chembl/api/data/status.json`
  (release), `/target/{id}.json`, `/molecule/{id}.json`,
  `/activity.json?target_chembl_id&limit&offset=0` (one page, limit at most
  100) and `/document/{id}.json` for the documents a page cites (at most 10)
  (field names *verify*).
- **Authentication:** none.
- **Licence:** Creative Commons Attribution-Share Alike 3.0 Unported, allowing
  use, redistribution and adaptation with attribution and share-alike; ChEMBL
  notes that compound property calculations from commercial software carry
  their own terms (extract of the ChEMBL interface documentation, About and
  FAQ). **Redistribution:** with attribution to ChEMBL and the release;
  adaptations share-alike. Computed properties are not stored.
- **Rate limits:** none could be read (**unverified**); five pages plus at most
  10 document requests per run.
- **Updates, corrections, removals:** numbered releases (`chembl_db_version`);
  every record is keyed by ChEMBL ID and release and each release is a
  revision. `data_validity_comment` and `data_validity_description` are ChEMBL's
  own flags on suspect values and are shown with every value. An activity
  absent from the next *complete* page of the same target selection gets a
  dated `removed` revision; a truncated page never removes anything.

## Sources recorded as not implemented

None. Every candidate source's terms allow the intended bounded, cited use.

## Bounded first coverage

| Source | Selection | Justification |
| --- | --- | --- |
| UniProt | one reviewed entry with its UniSave history, one unreviewed entry and one merged accession (fixture); live: at most 20 declared accessions | versions, labels, obsolescence and cross-references in one small set |
| NCBI | the genes and organisms those entries name, one replaced Gene ID and one merged Tax ID | successor handling and lineage |
| RCSB PDB | experimental entries the proteins cross-reference (at most 10 entities each) and one obsolete entry with its successor | revision history and supersession |
| ChEMBL | the proteins' ChEMBL targets, one activity page of at most 100 records per target, the compounds named and the documents cited, for one named release | published activity per release, far below any limit |

Places and periods do not apply; the period is the release read. Every
selection is explicit in the source pack; there is no crawl or search.
