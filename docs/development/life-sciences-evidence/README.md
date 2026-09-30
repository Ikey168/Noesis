# Life sciences: live-coverage evidence

This directory holds the source audit ([`source-audit.md`](source-audit.md)) and
dated live checks only. Offline fixture evidence lives in
`tests/unit/domains/test_lifesci_*.py` (the journey is
`tests/unit/domains/test_lifesci_acceptance.py`) and is never recorded here.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. UniProtKB, NCBI Gene, NCBI Taxonomy, RCSB PDB and ChEMBL stay `unverified-live` until a bounded live check (LS14, #2721) records dated counts per declared document, release stamps, response hashes and failure codes here. |
