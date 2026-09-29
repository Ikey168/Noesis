# Open-source Software Ecosystems: evidence

Tracking: [#2192](https://github.com/Ikey168/Noesis/issues/2192).

Offline and live evidence are kept apart.

- **Offline** (fixtures, no network, no credentials): the source audit
  ([source-audit.md](source-audit.md)) and the tests under
  `tests/unit/oss_ecosystems/`,
  `tests/unit/domains/test_oss_ecosystems_acceptance.py` and
  `tests/unit/composition/test_oss_ecosystems_composition.py`. Every fixture is
  an authored response in the provider's documented shape for fictional
  packages (`fixture-parser` on PyPI, `@fixture-labs/tokenizer` on npm,
  `fixture-codec` on crates.io, `org.fixturelabs:fixture-core` on Maven
  Central), fictional organisations and fictional repositories. None is a
  capture.
- **Live** (dated, bounded): none yet. Every source stays `unverified-live`
  until the live-validation issue (OS13,
  [#2205](https://github.com/Ikey168/Noesis/issues/2205)) records a dated run
  here with per-request outcomes, response hashes and failure codes.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists for any registry, deps.dev, the SPDX License List or Software Heritage. |
