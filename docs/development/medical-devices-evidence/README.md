# Medical devices evidence

Evidence for the Clinical Evidence pack's `clinical.devices` provider (#2654):
FDA device clearances, approvals with supplements, classifications, recalls
and MAUDE adverse-event reports (openFDA), AccessGUDID device identifiers and
the EUDAMED public actor, device and certificate modules.

* `source-audit.md` - MD01 (#2658) source contracts, licence and
  redistribution terms, rate limits, revision models, EUDAMED module
  availability, the data-minimisation decision, bounded coverage and
  `LIVE_VERIFICATION` per provider.
* Offline evidence: `tests/unit/domains/test_medical_devices_*.py` and
  `tests/unit/composition/test_clinical_devices_composition.py` over authored
  fixtures in `tests/fixtures/medical_devices/` and the pinned source-pack
  fixtures `tests/fixtures/source_packs/clinical-devices-*.json`
  (recomposed and checked by `tests/unit/medical_devices_fixture_builder.py`).
  Every device, organisation, identifier and report there is fictional
  (Exampla Medical Devices Inc., Northwind Medtech GmbH, K999001, P999001,
  product codes ZXA/ZXB, DI 00899999000011, SRN US-MF-000099902, notified body
  9999).
* Live evidence: none yet. Every provider is `unverified-live`; the dated live
  run and the cited demo belong to MD14 (#2723) and are recorded here,
  separately from the offline evidence, when they exist.
