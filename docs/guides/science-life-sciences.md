# Science: life-science reference data

The `science.life-sciences` provider of the Science bundle takes a protein, gene,
structure, taxon or compound to the reference records the biological databases
published: UniProtKB proteins, NCBI Gene and NCBI Taxonomy records, RCSB PDB
structures, and ChEMBL targets, compounds, activities and documents. Every record
carries its source, native accession, source release, version marker and as-of
time, with its cross-references exactly as published. It covers the
`life-sciences-reference` subdomain of the domain coverage program (ADR-005).

**Coverage status: offline only.** Every source is `unverified-live`. The
behaviour below is proven against synthetic fixtures
(`tests/unit/domains/test_lifesci_acceptance.py`). No bounded live run has been
recorded yet (LS14, #2721); live evidence will be kept in
[`docs/development/life-sciences-evidence/`](../development/life-sciences-evidence/README.md),
separate from offline evidence.

## What it will not do

- No biological or clinical inference, no drug-target, indication or disease
  claim, and no activity prediction, scoring or ranking.
- No conversion, normalisation or aggregation of activity values across assays;
  ChEMBL's derived `pchembl_value` is not stored.
- No sequence analysis beyond storage. A sequence is kept with the length and
  checksums the source publishes.
- No personal names. Citation authors, PDB depositors and ChEMBL document authors
  are dropped at acquisition, refused at write time and stripped from every tool
  output.
- No redistribution beyond each source's licence: UniProt CC BY 4.0, NCBI public
  domain, PDB CC0 1.0 and ChEMBL CC BY-SA 3.0, all to be re-verified live. See the
  [source audit](../development/life-sciences-evidence/source-audit.md).

## Enable it

The Science bundle has one optional feature per source, and all are off by
default: `life-sciences-uniprot`, `life-sciences-ncbi`, `life-sciences-pdb` and
`life-sciences-chembl`. Each feature binds entity identity, subscriptions and the
source-pack runtime. Chemicals, Clinical Evidence and Biodiversity are not
required. When one of them is not composed, its links are reported as
`provider_absent`.

The sources live in `config/source_packs/scientific.json`
(`primary-scientific-evidence` 1.2.0) under the `life-sciences` connector:

| Source id | Provider | What a document declares |
| --- | --- | --- |
| `uniprot-lifesci-proteins` | UniProtKB | up to 20 accessions |
| `ncbi-gene-lifesci` | NCBI Gene | up to 20 Gene IDs |
| `ncbi-taxonomy-lifesci` | NCBI Taxonomy | up to 20 Tax IDs |
| `rcsb-pdb-lifesci-structures` | RCSB PDB | one entry with its polymer entities, or a removed-entries list |
| `chembl-lifesci-bioactivity` | ChEMBL | a target, molecules, activities (capped at 200 per target) or documents; the declared release is checked against `status.json` |

NCBI accepts an optional API key, `NOESIS_NCBI_API_KEY`. It is sent only as the
`api_key` parameter and never appears in a URL, receipt or record.

## Records and versions

- **Versions.** UniProt entries keep their entry and sequence versions, and PDB
  entries their `major.minor` revision and full revision history. NCBI and
  ChEMBL publish no marker, so a changed record gets a new revision keyed by its
  content digest.
- **Release membership.** A record seen unchanged in a later release keeps one
  revision and gains that release. The answer "as of" a release is the revision
  in force at that release.
- **Status changes are revisions.** Merged, demerged and deleted UniProt entries,
  replaced or discontinued genes, merged Tax IDs and obsolete PDB entries become
  revisions that name the successors the source gives. Nothing is deleted. The
  same marker arriving with different content is recorded as a conflict.

## Ask

| Question | Tool |
| --- | --- |
| This protein as of UniProt `2099_01`, with its structures, gene and targets | `lifesci_entry_as_of(namespace, accession="X9EXA1", release="2099_01")` |
| A merged or obsolete accession | `lifesci_entry_as_of(namespace, accession="X9EXA2")` resolves to the successors and shows the history |
| Compounds with published activity against a target | `lifesci_compounds_for_target(namespace, target="CHEMBL9900001" or a UniProt accession, release="CHEMBL_99")` |
| An evidence bundle for either answer | `export_lifesci_evidence_bundle` |
| Source terms, bounded coverage and the minimisation decision | `lifesci_source_contracts` |

In an entry's cross-reference graph, each cross-reference is labelled with the
source that asserts it. The graph shows the entry's own cross-references, the
cross-references other sources publish that name the entry, identity matches with
their review state, and cross-pack links. The answer to a target query groups
activities by assay type and activity type, side by side. Each activity keeps its
relation, value, unit and data-validity comment as ChEMBL published them, and
cites the activity and its document. If a subject has no records, the answer is
`none_on_record`, which is not evidence that the source has none.

## Identity and links

`propose_lifesci_matches` proposes identity matches in this order:

1. Published cross-references (UniProt to PDB, GeneID and ChEMBL; PDB entities
   and ChEMBL target components to UniProt).
2. InChIKey assertions between ChEMBL compounds and Chemicals substances.
3. NCBI Tax IDs that Biodiversity taxon identities publish.
4. Exact scientific names, only as low-confidence candidates when no identifier
   connects the records.

Every match is proposed and nothing is accepted or merged automatically.
`review_lifesci_match` and `revert_lifesci_match` record entity-history
decisions. `list_lifesci_unmatched` keeps unmatched records visible.

`link_lifesci_records` links records to other packs:

| Linked records | Basis |
| --- | --- |
| Compounds to Chemicals substances | accepted InChIKey match |
| Compounds to medicinal products | the product states the compound's ChEMBL ID or InChIKey |
| Targets and proteins to medicines records | the medicines record cites the accession |
| Taxa to Biodiversity occurrences | accepted match |
| Entries to scholarly documents | DOI citation |

Each link names both revisions. A cited target that is not held is kept as
`target_missing`.

## Monitor

`create_lifesci_monitor` watches one accession, target or NCBI taxon through a
knowledge subscription; there is no new scheduler. `run_lifesci_monitor` emits
these notices:

- `new_entry`
- `entry_revised`, with the changed fields
- `entry_obsoleted`, with the successors
- `new_activity`
- `activity_revised`

Each notice cites the new and previous revisions. A release that changes nothing
emits nothing, and a restart replays without duplicates. `LifeSciMonitor.refresh`
re-reads a source within its page budget, idempotently. It writes one receipt per
run and waits when the provider answers with Retry-After.
