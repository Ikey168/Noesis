# Life sciences: live-coverage evidence

This directory holds the source audit ([`source-audit.md`](source-audit.md)) and
dated live checks only. Offline fixture evidence lives in
`tests/unit/domains/test_lifesci_*.py` (the journey is
`tests/unit/domains/test_lifesci_acceptance.py`) and is never recorded here.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. UniProt (`rest.uniprot.org`), NCBI Datasets v2 (`api.ncbi.nlm.nih.gov`), the RCSB PDB Data API (`data.rcsb.org`) and the ChEMBL web services (`www.ebi.ac.uk/chembl/api/data`) stay `unverified-live` until a bounded live check (LS14, #2721) records, per declared selection, the dated counts, the release or revision read (UniProt `X-UniProt-Release`, ChEMBL `chembl_db_version`, PDB major.minor revision), response hashes and failure codes here, and confirms every field marked *verify* in the audit. |
