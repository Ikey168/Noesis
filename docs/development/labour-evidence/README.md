# Labour statistics: live-coverage evidence

This directory holds the source audit ([`source-audit.md`](source-audit.md)) and
dated live checks only. Offline fixture evidence lives in
`tests/unit/domains/test_labour_*.py` (the journey is
`tests/unit/domains/test_labour_acceptance.py`) and is never recorded here.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. ILOSTAT, OECD, Eurostat LFS and the BLS Public Data API stay `unverified-live` until a bounded live check (LB13, #2493) records dated counts per declared document, release stamps, response hashes and failure codes here. |
