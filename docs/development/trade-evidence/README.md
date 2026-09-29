# Trade flows: live-coverage evidence

This directory holds the source audit ([`source-audit.md`](source-audit.md)) and
dated live checks only. Offline fixture evidence lives in
`tests/unit/domains/test_trade_*.py` (the journey is
`tests/unit/domains/test_trade_flows_acceptance.py`) and is never recorded here.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. UN Comtrade, Eurostat Comext and the WITS concordance files stay `unverified-live` until a bounded live check (TF12, #2556) records dated counts per selection, release stamps, response hashes and failure codes here. UNSD correlation tables are `operator-import`. |
