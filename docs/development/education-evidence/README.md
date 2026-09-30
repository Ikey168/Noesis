# Education statistics: live-coverage evidence

This directory holds the source audit ([`source-audit.md`](source-audit.md)) and
dated live checks only. Offline fixture evidence lives in
`tests/unit/domains/test_education_*.py` (the journey is
`tests/unit/domains/test_education_statistics_acceptance.py`) and is never
recorded here.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. US IPEDS, ETER, the UNESCO UIS API, OECD Education at a Glance (SDMX) and Eurostat R&D statistics (SDMX-CSV) stay `unverified-live` until a bounded live check (ED14, #2444) records dated counts per declared document, release stamps, response hashes and failure codes here. |
