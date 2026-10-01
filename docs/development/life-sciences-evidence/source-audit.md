# Life sciences: source audit and bounded provider coverage (LS01)

Tracking: #2652 · delivery issue #2656 · recorded 2026-09-30.

This audit sets out, per source, what the Science bundle's `science.life-sciences`
provider may acquire, how, and on what terms. **The terms, endpoints and rate
limits below were not re-verified live**: the providers' documentation and terms
pages (uniprot.org, ncbi.nlm.nih.gov, rcsb.org, ebi.ac.uk) were unreachable from
the authoring runtime (egress blocked), so this audit is written from the tracker's
source references and the providers' published documentation as the author knows
it. Every item marked _verify_ must be checked against the live documentation, the
live terms and a real response before the first dated live run (LS14, #2721). No
provider is `live` until that run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `PERSONAL_DATA_DECISION` in
`src/ingestion/lifesci_sources.py`; the MCP tool `lifesci_source_contracts`
returns them, and each source entry in `config/source_packs/scientific.json`
(pack `primary-scientific-evidence` 1.2.0) carries its
`life_sciences.live_verification` status. Coverage is recorded against the
existing Science pack; no new pack is created.

These non-goals apply to every source: no biological or clinical inference, no
activity prediction, scoring or ranking, no conversion, normalisation or
aggregation of activity values across assays, no sequence analysis beyond storage
(a sequence is kept with the length and checksums the source publishes; nothing is
computed from it), and no redistribution beyond each source's licence.

## Access decisions

| Source | Delivers | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- |
| UniProtKB (rest.uniprot.org) | protein entries by accession | `unverified-live` | Public REST API without authentication. Entry JSON field names (`entryAudit`, `uniProtKBCrossReferences`, `inactiveReason`) and the `X-UniProt-Release` / `X-UniProt-Release-Date` headers are _verify_ |
| NCBI Gene (eutils.ncbi.nlm.nih.gov) | gene summaries by Gene ID | `unverified-live` | Public E-utilities; optional API key. The `esummary` JSON fields `status` and `currentid` are _verify_ |
| NCBI Taxonomy (eutils.ncbi.nlm.nih.gov) | taxa by Tax ID with lineage | `unverified-live` | Public E-utilities `efetch` XML; `AkaTaxIds` behaviour for merged IDs is _verify_ |
| RCSB PDB (data.rcsb.org) | structure entries by PDB ID | `unverified-live` | Public Data API without authentication; `rcsb_accession_info`, `pdbx_audit_revision_history`, polymer entity identifiers and the `holdings/removed` response are _verify_ |
| ChEMBL (www.ebi.ac.uk/chembl) | targets, compounds, activities, documents | `unverified-live` | Public web services without authentication; `status.json`, `activity.json` `page_meta` and field names are _verify_ |

No source was judged unimplementable: every licence allows the intended use
(storage of bounded reference records with attribution), so no "not implemented"
provider contract is needed.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits and bounds |
| --- | --- | --- | --- | --- |
| UniProtKB | `GET https://rest.uniprot.org/uniprotkb/{accession}.json` | none | CC BY 4.0, attribution to the UniProt Consortium (_verify_); redistribution with attribution | no published hard limit, fair use (_verify_); one request per declared accession, at most 20 per document |
| NCBI Gene | `GET .../entrez/eutils/esummary.fcgi?db=gene&id={ids}&retmode=json` | none; optional `NOESIS_NCBI_API_KEY`, sent only as the `api_key` parameter and never written to a URL, receipt or record | NCBI molecular data are not subject to copyright (US government work); some submitted data may carry third-party rights (_verify_) | 3 requests/s without a key, 10 with one (_verify_); one request per document of at most 20 IDs |
| NCBI Taxonomy | `GET .../entrez/eutils/efetch.fcgi?db=taxonomy&id={ids}&retmode=xml` | as NCBI Gene | public domain (_verify_) | as NCBI Gene |
| RCSB PDB | `GET https://data.rcsb.org/rest/v1/core/entry/{id}`, `.../core/polymer_entity/{id}/{n}`, `.../holdings/removed/{id}` | none | CC0 1.0 under the wwPDB usage policy (_verify_) | no published hard limit (_verify_); one entry request plus one per declared polymer entity (at most 20) |
| ChEMBL | `GET https://www.ebi.ac.uk/chembl/api/data/{status,target/{id},molecule/{id},activity,document/{id}}.json` | none | CC BY-SA 3.0, attribution to ChEMBL (_verify_); share-alike applies to redistributed ChEMBL data | no published hard limit (_verify_); at most 200 activities per target document, and a larger total is refused rather than truncated |

## Updates, corrections and removals

| Source | Version marker | Release | Corrections and removals |
| --- | --- | --- | --- |
| UniProtKB | `entryAudit.entryVersion` (and `sequenceVersion`) | `X-UniProt-Release` (`YYYY_NN`), about every eight weeks | a correction is a new entry version; an inactive entry (`MERGED`, `DEMERGED`, `DELETED`) is a revision naming `mergeDemergeTo` successors |
| NCBI Gene | none published in the summary: content digest | operator-declared release date, else retrieval date (labelled) | `status` 1 (secondary) names `currentid`; `status` 2 is discontinued; both are revisions |
| NCBI Taxonomy | none published: content digest | operator-declared release date, else retrieval date | a merged Tax ID (answered under another taxon's `AkaTaxIds`) is a `merged` revision naming the survivor; a Tax ID NCBI does not return is reported in the receipt, never recorded as deleted |
| RCSB PDB | `major_revision.minor_revision`, with the full `pdbx_audit_revision_history` | weekly release (declared) | remediations are new revisions; obsolete entries are revisions naming `id_codes_replaced_by` |
| ChEMBL | content digest per record | `chembl_db_version` (`CHEMBL_NN`) from `status.json`, checked against the declared release | a changed value or `data_validity_comment` in a later release is a new revision; records absent from a later release are not deleted |

## Data minimisation

The sources publish personal names: UniProt reference author lists and submission
names, PDB `audit_author`, primary-citation authors and depositor names, and ChEMBL
document authors. Decision:

- **Stored:** accessions, versions, names of genes, proteins, taxa, targets and
  compounds, citation identifiers (PubMed ID, DOI, ChEMBL document ID) and titles.
- **Excluded:** every author, depositor, submitter, contact and ORCID field, and
  ChEMBL abstracts. The adapters drop them before a statement is built.
- **Enforcement:** `validate_statement` refuses any statement carrying a personal
  field anywhere (`personal_data`), and every MCP tool strips them again from its
  output.
- **Retention:** nothing personal is retained, so no retention period applies. Raw
  responses are not stored; only their SHA-256 digests are kept in receipts.
- **Who may query:** records carry no personal data; reads need
  `knowledge:lifesci:read` and namespace access.

## Bounded coverage

| Source | Entities | Releases and caps |
| --- | --- | --- |
| UniProtKB | a declared accession set anchored on target proteins of one organism (a pinned proteome slice or query result), at most 20 per document | the two most recent releases |
| NCBI Gene | Gene IDs the declared proteins cross-reference (`GeneID`) | at most 20 per document |
| NCBI Taxonomy | organisms of the declared proteins and their parents | at most 20 per document |
| RCSB PDB | entries the declared proteins cross-reference, with their polymer entities | at most 20 entities per entry |
| ChEMBL | targets whose components are the declared proteins, their activities, and the compounds and documents those activities name | at most 200 activities per target; the two most recent releases |

Full proteomes, sequence similarity searches, computed molecular properties
(ChEMBL `molecule_properties`, `pchembl_value`), clinical development phase
(`max_phase`, which belongs to Clinical Evidence) and any prediction are out of
scope. The live verification (LS14) uses exactly this coverage. No record set
implies complete coverage of any provider.

## Identity and links

Cross-source identity uses the cross-references the sources publish first
(UniProt to PDB, GeneID and ChEMBL; PDB entity to UniProt; ChEMBL target component
to UniProt). ChEMBL compounds meet Chemicals substances by standard InChIKey and
NCBI taxa meet Biodiversity taxa by a published NCBI Tax ID, both as reviewable
assertions; an exact scientific name is only a low-confidence candidate
(`src/kb/lifesci_identity.py`, LS07). Links to Chemicals, Clinical, Biodiversity
and literature rest on a citation, a shared identifier or an accepted match
(`src/kb/lifesci_links.py`, LS08).

## Gap against the existing stores

No existing store keeps accession-keyed reference entries with source release
membership and version markers, so the records get namespace-scoped `lifesci_*`
tables (`src/kb/lifesci_store.py`) in the registry-record shape, owned by the new
`science.life-sciences` provider of the existing Science pack.
