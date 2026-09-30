# Platform transparency evidence

Evidence for the OSINT pack's platform-transparency features (#2580), provider
`osint.platform-transparency`.

* `source-audit.md` - SP01 source contracts (EU DSA Transparency Database,
  Meta Ad Library API, Google political ads data, Lumen), what could and could
  not be read from official pages on 2026-09-30, licences and redistribution,
  token handling, rate limits, revision and removal models, the
  **data-minimisation decision** and the bounded first coverage. Lumen is
  recorded as **not implemented** with its reason.
* Offline evidence: `tests/unit/domains/test_platform_transparency_acceptance.py`
  and the other `test_platform_transparency_*` suites, over authored fixtures in
  `tests/fixtures/platform_transparency/` and the pinned source-pack fixtures
  `tests/fixtures/source_packs/osint-platform-transparency-*.json` (fictional
  platforms, pages, advertisers and funders; withheld columns carry synthetic
  placeholders that the parser discards).
* Live evidence: none yet. Every implemented source is `unverified-live`; the
  dated bounded live runs and the cited demo belong to SP14 (#2650) and are
  recorded here, separately from the offline evidence, when they exist.
