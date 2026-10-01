# Medical devices evidence

Evidence for the Clinical Evidence pack's `clinical.devices` provider (#2654).

* `source-audit.md` - MD01 source contracts (openFDA device endpoints,
  AccessGUDID, EUDAMED public modules), key handling, licences and disclaimers,
  rate limits, revision models, EUDAMED module availability, the
  **data-minimisation decision** and the bounded first coverage.
* Offline evidence: `tests/unit/domains/test_medical_devices_sources.py`,
  `tests/unit/domains/test_medical_devices_acceptance.py` and the other
  `test_medical_devices_*` suites, over authored fixtures in
  `tests/fixtures/medical_devices/` (fictional companies, devices and
  identifiers; placeholder contact persons, addresses and patient blocks that
  the parsers drop).
* Live evidence: none yet. Every source is `unverified-live`; the dated live
  run and cited demo belong to MD14 (#2723) and are recorded here, separately
  from the offline evidence, when they exist.
