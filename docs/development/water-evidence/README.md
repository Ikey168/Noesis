# Water and hydrology: evidence

- [Source contract audit, data-minimisation decision, GRDC decision and bounded coverage](source-audit.md)
  (WA01, #2587).

## Live evidence

This section holds dated live checks only.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. PEGELONLINE, the USGS Water Data OGC API and the EEA WISE WFD Discodata views stay `unverified-live` until the bounded live validation (WA13, #2647) records dated counts per selection, response hashes and failure codes here. GRDC is `not-implemented`. The optional USGS key (`NOESIS_USGS_WATER_API_KEY`) is not configured here. |

## Offline evidence

Offline evidence is fixture-backed and lives in the tests, never here:
`tests/unit/water/` (per issue) and the journey
`tests/unit/domains/test_water_acceptance.py` (WA12, #2642), over the pinned
fixtures `tests/fixtures/source_packs/water-*.json` and
`tests/fixtures/water/later_payloads.json`. Stations, identifiers, codes,
coordinates, values and statuses in those fixtures are fictional, not live
evidence.
