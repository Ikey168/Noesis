# Materials evidence

- `source-audit.md` records the source contracts, licence decisions and bounded
  v1 coverage (MT01, #2079). Unverified claims are marked _(verify)_.
- Live checks only go here as `live-check-<date>.json` (MT14, #2092). None has
  been run: every provider in `LIVE_VERIFICATION`
  (`src/ingestion/materials_sources.py`) is `unverified`.

Offline fixture evidence lives in `tests/unit/materials` and
`tests/unit/domains/test_materials_acceptance.py` and is never recorded here.
