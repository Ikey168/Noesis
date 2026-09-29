# Fisheries and Maritime Activity: evidence

- [Source contract audit and bounded coverage](source-audit.md) (FI01, #2305).

## Live evidence

This section holds dated live checks only.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. Global Fishing Watch, FAO FishStat, the ICCAT, WCPFC and IOTC registers and IUU lists, and the Combined IUU Vessel List stay `unverified-live` until the bounded live validation (FI14, #2346) records dated counts per selection, snapshot dates, response hashes and failure codes here. |

## Offline evidence

Offline evidence is fixture-backed and lives in the tests, never here:
`tests/unit/fisheries/` (per issue) and the journey
`tests/unit/domains/test_fisheries_acceptance.py` (FI13, #2343), over the pinned
fixtures `tests/fixtures/source_packs/fisheries-*.json` and
`tests/fixtures/fisheries/later_payloads.json`. Every vessel in those fixtures is
synthetic.
