# Biodiversity: evidence

- [Source contract audit, IUCN licence decision, sensitive-species policy and bounded coverage](source-audit.md)
  (BD01, #2504).

## Live evidence

This section holds dated live checks only.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. Catalogue of Life (ChecklistBank), the GBIF species, occurrence, dataset and download-metadata APIs and the IUCN Red List API stay `unverified-live` until the bounded live validation (BD13, #2533) records dated counts per selection, release versions, response hashes and failure codes here. GBIF download creation is `not-implemented`; IUCN needs the `NOESIS_IUCN_API_TOKEN` credential. |

## Offline evidence

Offline evidence is fixture-backed and lives in the tests, never here:
`tests/unit/biodiversity/` (per issue) and the journey
`tests/unit/domains/test_biodiversity_acceptance.py` (BD12, #2531), over the
pinned fixtures `tests/fixtures/source_packs/biodiversity-*.json` and
`tests/fixtures/biodiversity/later_payloads.json`. Identifiers, counts,
coordinates and categories in those fixtures are illustrative, not live
evidence.
