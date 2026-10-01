# Science life-sciences guide

The `science.life-sciences` provider of the Science bundle returns the
reference records biological databases published for a protein, gene,
structure, taxon or compound, each with its source, release or entry version
and as-of time: UniProtKB entries (UniProt), genes and taxa (NCBI Gene and
Taxonomy), experimental structures (RCSB PDB) and targets, compounds and
published activities (ChEMBL). It fills the `life-sciences-reference`
subdomain of the [domain coverage program](../roadmaps/domain-coverage-program.md)
(tracker #2652). Coverage is **offline** (fixture-tested) until the live
validation issue (#2721) records a dated run in
[`docs/development/life-sciences-evidence/`](../development/life-sciences-evidence/README.md).

## What it never does

No biological or clinical inference, no activity prediction, no sequence
analysis beyond storage, no conversion or aggregation of activity values
across assays or assay types, no redistribution beyond each source's licence
and no person names (author, depositor and submitter fields are dropped at
acquisition; see the [source audit](../development/life-sciences-evidence/source-audit.md)).
Every answering tool declares these exclusions and refuses to return a
predicted, converted or personal field.

## Enabling

Each source is its own optional Science feature, default off:
`life-sciences-uniprot`, `life-sciences-ncbi`, `life-sciences-pdb` and
`life-sciences-chembl`. Each binds entity identity, subscriptions and the
source-pack runtime. Chemicals, Biodiversity and Clinical medicines are never
required: when installed, links reach them; when absent, `link_lifesci_records`
reports them as missing.

Acquisition runs through the source-pack tools on `primary-scientific-evidence`
(sources `uniprot-proteins`, `ncbi-genes-taxonomy`, `rcsb-pdb-structures`,
`chembl-bioactivity`) after accepting each source's licence. The optional NCBI
API key is the `NOESIS_NCBI_API_KEY` secret and travels only in the `api-key`
header. Selections are explicit and bounded (see the audit).

## Records and revisions

- A UniProt entry keeps its accession, reviewed/unreviewed label as UniProt
  states it, entry and sequence versions and the release it was read in; the
  UniSave history names the version in force at each release. A merged,
  demerged or deleted accession is an `obsoleted` revision naming its
  successors.
- An NCBI gene keeps its Gene ID and status (replaced genes name the current
  Gene ID); a taxon keeps its Tax ID, rank and lineage as published (merged Tax
  IDs name the node they were merged into).
- A PDB entry keeps its revision history, methods and resolution as published
  and each polymer entity's UniProt mapping as the PDB states it; an obsolete
  entry names the entries that supersede it.
- ChEMBL records are keyed by ChEMBL ID and release. Activities keep the
  published and ChEMBL-standardised type, relation, value and unit as strings,
  with data-validity and activity comments, and cite their document.

Records are immutable revisions; a removal or correction by the source is a
new revision, never a deletion.

## Journey: protein to cited reference records

1. `propose_lifesci_identity_matches` offers matches from published
   cross-references first (UniProt to PDB, GeneID and ChEMBL targets), ChEMBL
   compounds to Chemicals substances by InChIKey and NCBI taxa to Biodiversity
   taxa by published Tax ID (exact names only when no identifier connects
   taxa, at low confidence). Nothing is accepted or merged; unmatched records
   are listed.
2. `review_lifesci_identity_match` accepts or rejects with a reason (an entity
   identity decision); `revert_lifesci_identity_match` undoes it.
3. `link_lifesci_records` links record revisions to Chemicals substances and
   Biodiversity occurrences (accepted matches only), Clinical medicines whose
   regulatory records name the ChEMBL ID, and papers citing the same DOI or
   PubMed ID.
4. `lifesci_entry_as_of(namespace, identifier, release=..., as_of=...)` returns
   the entry version in force, obsolete identifiers resolved to successors with
   the history shown, and the cross-reference graph labelled by the asserting
   source with identity states and links; every entry version is cited.
5. `lifesci_target_activities(namespace, target, release=...)` lists the
   compounds and activities ChEMBL published for the target in a release, each
   citing its activity and document; removed activities are listed apart.
6. `export_lifesci_evidence_bundle` turns either answer into a
   `noesis-evidence-bundle-v1` citing every revision with source, release and
   as-of time.
7. `create_lifesci_monitor` / `run_lifesci_monitor` / `poll_lifesci_monitor`
   watch accessions, targets or taxa and notify new, revised, obsoleted, added
   and removed records once, citing prior and new revisions; unchanged
   republications emit nothing.

A subject with no records answers `not_on_record`: a statement about the
bounded, acquired selection, not about the source.

## Evidence

Offline: `tests/unit/domains/test_lifesci_*.py` (journey:
`test_lifesci_acceptance.py`) and
`tests/unit/composition/test_science_life_sciences_composition.py`, with
synthetic fixtures under `tests/fixtures/source_packs/lifesci-*.json` and
`tests/fixtures/lifesci/` (rebuilt by `python -m tests.unit.lifesci_fixture_builder`).
Live: none yet.
