# Water and hydrology: evidence

- [Source contract audit, GRDC decision, personal-data minimisation and bounded coverage](source-audit.md)
  (WA01, #2587).

## Live evidence

This section holds dated live checks only.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. PEGELONLINE, the USGS Water Data APIs and EEA WISE (Discodata SQL and the WISE map service) stay `unverified-live` until the bounded live validation (WA13, #2647) records dated counts per selection, response hashes and failure codes here. The official documentation pages could not be read directly from this runtime (egress proxy); see the audit. GRDC is `not-implemented` (terms forbid redistribution). |

## Offline evidence

Offline evidence is fixture-backed and lives in the tests, never here:
`tests/unit/domains/test_water_*.py` (per issue) and the journey
`tests/unit/domains/test_water_acceptance.py` (WA12, #2642), over the pinned
fixtures `tests/fixtures/source_packs/water-*.json` and
`tests/fixtures/water/later_payloads.json` (built by
`tests/unit/water/fixture_builder.py`). Station identifiers, water-body codes,
coordinates, values and status labels in those fixtures are illustrative, not
live evidence.
