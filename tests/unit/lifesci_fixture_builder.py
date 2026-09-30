"""Builds the authored life-sciences fixtures (#2652): SYNTHETIC payloads in each provider's documented shape.

Run ``python -m tests.unit.lifesci_fixture_builder`` to rewrite
``tests/fixtures/source_packs/lifesci-*.json`` and
``tests/fixtures/lifesci/later_payloads.json``. Accessions, Gene IDs, Tax IDs,
PDB IDs, ChEMBL IDs, InChIKeys, DOIs and PubMed IDs are fictional (valid
formats only); author lists are present only to prove they are dropped and use
placeholder names, not real people.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTE = ("Authored, not captured: SYNTHETIC records in the provider's documented response shape. Identifiers, "
        "names, sequences and values are fictional; person fields are placeholders present only to prove they are "
        "dropped; unverified-live (#2721).")
ORGANISM = "Exemplomyces fictus"
TAX = 999001
SEQ_V2 = "MKTAYIAKQRQISFVKSHFSRQ"
SEQ_V3 = "MKTAYIAKQRQISFVKSHFSRQLEERLGL"


def uniprot_entry(release: str, version: int, seq_version: int, sequence: str, *, updated: str) -> dict:
    return {
        "request": "/uniprotkb/P0DZZ1?format=json",
        "headers": {"X-UniProt-Release": release, "X-UniProt-Release-Date": updated},
        "body": {
            "entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P0DZZ1",
            "secondaryAccessions": ["Q9ZZZ1"], "uniProtkbId": "FKA1_EXEFI",
            "entryAudit": {"firstPublicDate": "2090-05-01", "lastAnnotationUpdateDate": updated,
                           "lastSequenceUpdateDate": "2098-01-10" if seq_version == 2 else updated,
                           "entryVersion": version, "sequenceVersion": seq_version},
            "annotationScore": 5.0,
            "organism": {"scientificName": ORGANISM, "taxonId": TAX,
                         "lineage": ["Eukaryota", "Fungi", "Exemplomyces"]},
            "proteinDescription": {"recommendedName": {"fullName": {"value": "Fictional kinase A"}}},
            "genes": [{"geneName": {"value": "FKA1"}}],
            "comments": [{"commentType": "FUNCTION", "texts": [{"value": "Curated function text (not stored)."}]}],
            "features": [{"type": "Domain", "location": {"start": {"value": 1}, "end": {"value": 20}}}],
            "keywords": [{"id": "KW-0418", "name": "Kinase"}],
            "references": [{"referenceNumber": 1, "citation": {
                "id": "99990011", "citationType": "journal article",
                "authors": ["Placeholder A.", "Placeholder B."],
                "citationCrossReferences": [{"database": "PubMed", "id": "99990011"},
                                            {"database": "DOI", "id": "10.5555/fict.lifesci.2099.1"}],
                "title": "A fictional kinase from a fictional fungus.", "publicationDate": "2091",
                "journal": "J. Fict. Biol."}}],
            "uniProtKBCrossReferences": [
                {"database": "PDB", "id": "9ZZ2", "properties": [{"key": "Method", "value": "X-ray"},
                                                                 {"key": "Resolution", "value": "2.10 A"}]},
                {"database": "PDB", "id": "9ZZ3", "properties": [{"key": "Method", "value": "EM"}]},
                {"database": "GeneID", "id": "99990001", "properties": []},
                {"database": "ChEMBL", "id": "CHEMBL9990201", "properties": []},
            ],
            "sequence": {"value": sequence, "length": len(sequence), "molWeight": 2500 + len(sequence),
                         "crc64": "FICTCRC64" + str(seq_version), "md5": "f1c7" + str(seq_version) * 28},
        },
    }


def uniprot() -> dict:
    return {
        "authored": True, "captured": None, "note": NOTE, "provider": "uniprot",
        "scenarios": [
            ("a reviewed entry with entry and sequence versions, the release header, cross-references to PDB, "
             "GeneID and ChEMBL and a citation whose author list is dropped"),
            "the UniSave history of that entry (three versions over four releases)",
            "an unreviewed (TrEMBL) entry without cross-references",
            "a merged accession kept as an obsoleted revision naming its successor",
        ],
        "native_pages": [
            uniprot_entry("2099_01", 20, 2, SEQ_V2, updated="2099-01-15"),
            {"request": "/unisave/P0DZZ1?format=json", "body": {"results": [
                {"accession": "P0DZZ1", "database": "Swiss-Prot", "entryVersion": 20, "sequenceVersion": 2,
                 "firstRelease": "2099_01", "firstReleaseDate": "2099-01-15", "lastRelease": "2099_01",
                 "lastReleaseDate": "2099-01-15", "name": "FKA1_EXEFI"},
                {"accession": "P0DZZ1", "database": "Swiss-Prot", "entryVersion": 19, "sequenceVersion": 2,
                 "firstRelease": "2098_05", "firstReleaseDate": "2098-10-01", "lastRelease": "2098_06",
                 "lastReleaseDate": "2098-12-01", "name": "FKA1_EXEFI"},
                {"accession": "P0DZZ1", "database": "Swiss-Prot", "entryVersion": 18, "sequenceVersion": 1,
                 "firstRelease": "2098_01", "firstReleaseDate": "2098-02-01", "lastRelease": "2098_04",
                 "lastReleaseDate": "2098-08-01", "name": "FKA1_EXEFI"},
            ]}},
            {"request": "/uniprotkb/A0AZZ1ZZZ2?format=json",
             "headers": {"X-UniProt-Release": "2099_01", "X-UniProt-Release-Date": "2099-01-15"},
             "body": {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "A0AZZ1ZZZ2",
                      "uniProtkbId": "A0AZZ1ZZZ2_EXEFI",
                      "entryAudit": {"firstPublicDate": "2097-03-01", "lastAnnotationUpdateDate": "2099-01-15",
                                     "lastSequenceUpdateDate": "2097-03-01", "entryVersion": 3,
                                     "sequenceVersion": 1},
                      "organism": {"scientificName": ORGANISM, "taxonId": TAX},
                      "proteinDescription": {"submissionNames": [{"fullName": {"value": "Uncharacterized protein"}}]},
                      "sequence": {"value": "MSTNPKPQRKTKRNTNRRPQDVKF", "length": 24, "molWeight": 2800,
                                   "crc64": "FICTCRC64U", "md5": "0" * 32}}},
            {"request": "/uniprotkb/Q9ZZZ1?format=json",
             "headers": {"X-UniProt-Release": "2099_01", "X-UniProt-Release-Date": "2099-01-15"},
             "body": {"entryType": "Inactive", "primaryAccession": "Q9ZZZ1", "uniProtkbId": "Q9ZZZ1_EXEFI",
                      "inactiveReason": {"inactiveReasonType": "MERGED", "mergeDemergeTo": ["P0DZZ1"]}}},
        ],
    }


def ncbi() -> dict:
    lineage = {"domain": {"id": 2759, "name": "Eukaryota"}, "kingdom": {"id": 4751, "name": "Fungi"},
               "genus": {"id": 999000, "name": "Exemplomyces"}, "species": {"id": TAX, "name": ORGANISM}}
    taxon = {"tax_id": TAX, "rank": "SPECIES",
             "current_scientific_name": {"name": ORGANISM, "authority": "Fictor 2090"},
             "classification": lineage, "parents": [1, 131567, 2759, 4751, 999000], "genetic_code_id": 1}
    return {
        "authored": True, "captured": None, "note": NOTE, "provider": "ncbi",
        "scenarios": [
            "a live gene naming its Swiss-Prot accession",
            "a replaced Gene ID kept with the Gene ID NCBI names as current",
            "a taxon with its lineage as published",
            "a merged Tax ID answered with its current node",
        ],
        "native_pages": [
            {"request": "/datasets/v2/gene/id/99990001", "body": {"reports": [{"query": ["99990001"], "gene": {
                "gene_id": "99990001", "symbol": "FKA1", "description": "fictional kinase A", "tax_id": str(TAX),
                "taxname": ORGANISM, "type": "PROTEIN_CODING", "chromosomes": ["IV"],
                "swiss_prot_accessions": ["P0DZZ1"], "ensembl_gene_ids": ["EXEFI00000001"]}}], "total_count": 1}},
            {"request": "/datasets/v2/gene/id/99990009", "body": {"reports": [{"query": ["99990009"], "warning": {
                "gene_warning_code": "REPLACED", "symbol": "FKA1-OLD", "tax_id": str(TAX),
                "message": "Gene ID 99990009 was replaced by 99990001",
                "replaced_id": {"gene_id": "99990001"}}}], "total_count": 1}},
            {"request": f"/datasets/v2/taxonomy/taxon/{TAX}", "body": {"reports": [
                {"query": [str(TAX)], "taxonomy": taxon}], "total_count": 1}},
            {"request": "/datasets/v2/taxonomy/taxon/999002", "body": {"reports": [
                {"query": ["999002"], "taxonomy": taxon}], "total_count": 1}},
        ],
    }


def pdb_entry(pdb_id: str, *, title: str, method: str, resolution: float, revisions: list[tuple], entity_ids,
              supersedes: str | None = None) -> dict:
    last = revisions[-1]
    body = {
        "rcsb_id": pdb_id, "struct": {"title": title}, "exptl": [{"method": method}],
        "rcsb_entry_info": {"resolution_combined": [resolution], "experimental_method": method.title()},
        "rcsb_accession_info": {"deposit_date": "2098-11-20T00:00:00+0000",
                                "initial_release_date": revisions[0][3] + "T00:00:00+0000",
                                "revision_date": last[3] + "T00:00:00+0000", "major_revision": last[1],
                                "minor_revision": last[2], "status_code": "REL"},
        "pdbx_audit_revision_history": [{"ordinal": o, "major_revision": ma, "minor_revision": mi,
                                         "revision_date": d + "T00:00:00+0000", "data_content_type": "Structure model"}
                                        for o, ma, mi, d, _ in revisions],
        "pdbx_audit_revision_details": [{"ordinal": o, "revision_ordinal": o, "type": t,
                                         "data_content_type": "Structure model"} for o, _, _, _, t in revisions],
        "audit_author": [{"name": "Placeholder, A.", "pdbx_ordinal": 1}],
        "rcsb_primary_citation": {"title": f"Structure of {title} (fictional)", "journal_abbrev": "Fict. Struct.",
                                  "year": 2099, "pdbx_database_id_DOI": f"10.5555/fict.lifesci.pdb.{pdb_id.lower()}",
                                  "pdbx_database_id_PubMed": 99990012,
                                  "rcsb_authors": ["Placeholder, A."]},
        "rcsb_entry_container_identifiers": {"entry_id": pdb_id, "polymer_entity_ids": entity_ids},
    }
    if supersedes:
        body["pdbx_database_PDB_obs_spr"] = [{"id": "SPRSDE", "pdb_id": pdb_id, "replace_pdb_id": supersedes,
                                              "date": revisions[0][3] + "T00:00:00+0000"}]
    return {"request": f"/rest/v1/core/entry/{pdb_id}", "body": body}


def entity(pdb_id: str, entity_id: str, description: str, accessions: list[str]) -> dict:
    return {"request": f"/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}", "body": {
        "rcsb_id": f"{pdb_id}_{entity_id}", "rcsb_polymer_entity": {"pdbx_description": description},
        "rcsb_polymer_entity_container_identifiers": {
            "entry_id": pdb_id, "entity_id": entity_id, "uniprot_ids": accessions,
            "reference_sequence_identifiers": [{"database_name": "UniProt", "database_accession": a}
                                               for a in accessions]}}}


PDB_9ZZ2_REVISIONS = [(1, 1, 0, "2099-01-05", "Initial release"), (2, 1, 1, "2099-02-01", "Data collection")]


def pdb() -> dict:
    return {
        "authored": True, "captured": None, "note": NOTE, "provider": "pdb",
        "scenarios": [
            "an X-ray entry with its revision history, resolution as published and an entity mapped to UniProt",
            "an EM entry that supersedes an obsolete entry",
            "the obsolete entry from the removed holdings naming its successor",
            "the audit author list is dropped",
        ],
        "native_pages": [
            pdb_entry("9ZZ2", title="Fictional kinase A in complex with fictinib", method="X-RAY DIFFRACTION",
                      resolution=2.1, revisions=PDB_9ZZ2_REVISIONS, entity_ids=["1"]),
            entity("9ZZ2", "1", "Fictional kinase A", ["P0DZZ1"]),
            pdb_entry("9ZZ3", title="Fictional kinase A dimer", method="ELECTRON MICROSCOPY", resolution=3.4,
                      revisions=[(1, 1, 0, "2099-01-12", "Initial release")], entity_ids=["1", "2"],
                      supersedes="9ZZ1"),
            entity("9ZZ3", "1", "Fictional kinase A", ["P0DZZ1"]),
            entity("9ZZ3", "2", "Fictional nanobody", []),
            {"request": "/rest/v1/holdings/removed/9ZZ1", "body": {
                "rcsb_id": "9ZZ1", "rcsb_repository_holdings_removed": {
                    "status_code": "OBS", "remove_date": "2099-01-12T00:00:00+0000",
                    "id_codes_replaced_by": ["9ZZ3"], "title": "Fictional kinase A dimer (superseded)"}}},
        ],
    }


def activity(activity_id, molecule, doc, *, kind, relation, value, units, std_value, std_units=None, assay="B",
             validity=None, comment=None, pchembl=None) -> dict:
    return {"activity_id": activity_id, "assay_chembl_id": "CHEMBL9990401", "assay_type": assay,
            "assay_description": "Inhibition of fictional kinase A (fictional assay)",
            "target_chembl_id": "CHEMBL9990201", "molecule_chembl_id": molecule, "document_chembl_id": doc,
            "type": kind, "relation": relation, "value": value, "units": units, "standard_type": kind,
            "standard_relation": relation, "standard_value": std_value, "standard_units": std_units or units, "standard_flag": 1,
            "data_validity_comment": validity,
            "data_validity_description": "Values for this activity type are outside the typical range"
            if validity else None, "activity_comment": comment, "document_journal": "J. Fict. Med. Chem.",
            "document_year": 2098, "pchembl_value": pchembl}


A1 = activity(99990001, "CHEMBL9990101", "CHEMBL9990301", kind="IC50", relation="=", value="12", units="nM",
              std_value="12.0", pchembl="7.92")
A2 = activity(99990002, "CHEMBL9990102", "CHEMBL9990301", kind="Ki", relation=">", value="10", units="uM",
              std_value="10000.0", std_units="nM", validity="Outside typical range")
A3 = activity(99990003, "CHEMBL9990101", "CHEMBL9990302", kind="Inhibition", relation="=", value="45", units="%",
              std_value="45.0", assay="F", comment="Active")
A4 = activity(99990004, "CHEMBL9990102", "CHEMBL9990302", kind="IC50", relation="=", value="8.5", units="nM",
              std_value="8.5")
ACTIVITY_REQUEST = "/chembl/api/data/activity.json?limit=10&offset=0&target_chembl_id=CHEMBL9990201"


def status(release: str, date: str) -> dict:
    return {"request": "/chembl/api/data/status.json",
            "body": {"chembl_db_version": release, "chembl_release_date": date, "status": "UP"}}


def activities(rows) -> dict:
    return {"request": ACTIVITY_REQUEST, "body": {"activities": rows, "page_meta": {
        "limit": 10, "offset": 0, "next": None, "previous": None, "total_count": len(rows)}}}


def chembl() -> dict:
    return {
        "authored": True, "captured": None, "note": NOTE, "provider": "chembl",
        "scenarios": [
            "release ChEMBL_99 read first; every record keyed by ChEMBL ID and release",
            "a single-protein target naming its UniProt component",
            "two compounds with standard InChIKeys (computed properties and max_phase dropped)",
            ("three activities with published and standardised values as strings, a data-validity comment and an "
             "activity comment; pChEMBL values dropped"),
            "the two cited documents (author lists dropped; one without DOI)",
        ],
        "native_pages": [
            status("ChEMBL_99", "2099-01-20"),
            {"request": "/chembl/api/data/target/CHEMBL9990201.json", "body": {
                "target_chembl_id": "CHEMBL9990201", "pref_name": "Fictional kinase A", "target_type": "SINGLE PROTEIN",
                "organism": ORGANISM, "tax_id": TAX, "species_group_flag": False,
                "target_components": [{"accession": "P0DZZ1", "component_id": 1, "component_type": "PROTEIN",
                                       "relationship": "SINGLE PROTEIN"}],
                "cross_references": [{"xref_src": "UniProt", "xref_id": "P0DZZ1", "xref_name": None}]}},
            {"request": "/chembl/api/data/molecule/CHEMBL9990101.json", "body": {
                "molecule_chembl_id": "CHEMBL9990101", "pref_name": "FICTINIB", "molecule_type": "Small molecule",
                "max_phase": None, "molecule_properties": {"full_mwt": "300.00", "alogp": "2.00"},
                "molecule_structures": {"standard_inchi_key": "ZZZZZZZZZZZZZA-UHFFFAOYSA-N",
                                        "standard_inchi": "InChI=1S/fictional/a", "canonical_smiles": "C1=CC=CC=C1"},
                "cross_references": [{"xref_src": "PubChem", "xref_id": "999001", "xref_name": "SID: 999001"}]}},
            {"request": "/chembl/api/data/molecule/CHEMBL9990102.json", "body": {
                "molecule_chembl_id": "CHEMBL9990102", "pref_name": None, "molecule_type": "Small molecule",
                "molecule_properties": {"full_mwt": "320.00"},
                "molecule_structures": {"standard_inchi_key": "ZZZZZZZZZZZZZB-UHFFFAOYSA-N",
                                        "standard_inchi": "InChI=1S/fictional/b", "canonical_smiles": "C1CCCCC1"},
                "cross_references": []}},
            activities([A1, A2, A3]),
            {"request": "/chembl/api/data/document/CHEMBL9990301.json", "body": {
                "document_chembl_id": "CHEMBL9990301", "doi": "10.5555/fict.lifesci.2099.3", "pubmed_id": 99990013,
                "title": "Fictinib, a fictional kinase A inhibitor", "journal": "J. Fict. Med. Chem.", "year": 2098,
                "volume": "12", "first_page": "101", "doc_type": "PUBLICATION",
                "authors": "Placeholder A, Placeholder B"}},
            {"request": "/chembl/api/data/document/CHEMBL9990302.json", "body": {
                "document_chembl_id": "CHEMBL9990302", "doi": None, "pubmed_id": None,
                "title": "Fictional screening dataset", "journal": None, "year": 2098, "doc_type": "DATASET",
                "authors": None}},
        ],
    }


def later() -> dict:
    """Pages that change in the next releases (UniProt 2099_02, PDB revision 1.2, ChEMBL_100)."""
    return {
        "note": NOTE,
        "uniprot": {"/uniprotkb/P0DZZ1?format=json": {
            k: v for k, v in uniprot_entry("2099_02", 21, 3, SEQ_V3, updated="2099-02-20").items() if k != "request"}},
        "pdb": {"/rest/v1/core/entry/9ZZ2": {k: v for k, v in pdb_entry(
            "9ZZ2", title="Fictional kinase A in complex with fictinib", method="X-RAY DIFFRACTION", resolution=2.1,
            revisions=[*PDB_9ZZ2_REVISIONS, (3, 1, 2, "2099-03-01", "Structure summary")],
            entity_ids=["1"]).items() if k != "request"}},
        "chembl": {
            "/chembl/api/data/status.json": {"body": {"chembl_db_version": "ChEMBL_100",
                                                      "chembl_release_date": "2099-07-01", "status": "UP"}},
            ACTIVITY_REQUEST: {k: v for k, v in activities([A1, A3, A4]).items() if k != "request"},
        },
    }


def write() -> None:
    out = ROOT / "tests/fixtures/source_packs"
    for name, build in (("uniprot", uniprot), ("ncbi", ncbi), ("pdb", pdb), ("chembl", chembl)):
        (out / f"lifesci-{name}.json").write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n")
    folder = ROOT / "tests/fixtures/lifesci"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "later_payloads.json").write_text(json.dumps(later(), indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    write()
