# Public Procurement live-coverage evidence

This directory holds live checks only. Offline fixture evidence lives in
`tests/unit/procurement` and `tests/unit/domains/test_procurement_acceptance.py`
and is never recorded here.

`scripts/procurement_live_check.py` writes `live-check-<date>.json`. For each
implemented provider it runs one bounded source through the real source-pack
runtime (`network: live`, one page) and records:

- the runtime preflight (network policy, credential, licence);
- the source receipt (status, pages, records, failure code and
  classification);
- a direct adapter probe with the exact error code and transport detail;
- when notices were read, their deadlines as published and requirement quotes
  to check by hand against the official notice.

| Run | Result |
| --- | --- |
| `live-check-2026-09-27.json` | No provider was reached, so coverage is **not** validated. TED, Find a Tender and Contracts Finder failed with `source_unavailable`, because the build environment's egress proxy refused CONNECT to their hosts (`Tunnel connection failed: 403 Forbidden`). SAM.gov was blocked at preflight with `credential_missing`: `NOESIS_SAM_API_KEY` is not configured and no request was sent. Rerun from a network that can reach api.ted.europa.eu, www.find-tender.service.gov.uk, www.contractsfinder.service.gov.uk and api.sam.gov, with a SAM.gov key. |
