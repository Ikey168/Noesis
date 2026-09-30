# Critical infrastructure registries: evidence

- [Source contract audit and bounded coverage](source-audit.md) (CI01, #2359).

## Live evidence

This section holds dated live checks only.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists. GPPD, the GEM tracker releases, the Overpass extracts, the EIA layers and ENTSOG stay `unverified-live` until the bounded live validation (CI14, #2401) records the following here: dated counts per selection, release labels, Overpass `timestamp_osm_base`, response hashes and failure codes. |

## Offline evidence

Offline evidence is fixture-backed and lives in the tests, never here:

- `tests/unit/infrastructure/` (per issue);
- the journey `tests/unit/domains/test_infrastructure_acceptance.py` (CI13, #2398).

These run over the pinned fixtures `tests/fixtures/source_packs/infrastructure-*.json` and
`tests/fixtures/infrastructure/*.json`. Every asset, owner and identifier in those fixtures is
synthetic. They are authored in each publisher's documented response shape and are not captures.
