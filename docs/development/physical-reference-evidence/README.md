# Physical sciences reference evidence (science.physical-reference)

Evidence for the Science bundle's proposed `science.physical-reference`
provider (subdomain `physical-sciences-reference`, issue #2818, wave 3 tracker
#2814; no per-track tracker yet).

- [source-audit.md](source-audit.md) - PR01: per-source contract, licence,
  access, rate limits, revision model (editions side by side), the value
  representation (published decimal strings with uncertainty and unit), the
  data-minimisation decision and the bounded first coverage. Written without
  network access (publisher hosts blocked, 2026-11-01); terms not re-verified
  live. NIST CODATA constants, the NIST Atomic Spectra Database and IAEA
  LiveChart are `unverified-live`; PDG summary tables are `unverified-live`
  conditional on the PDG data licence. Record shape: `registry-records`; no
  new shape proposed.
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/physical_reference_sources.py` exist.
- Live evidence: none. Until a dated live run exists every source is
  `unverified-live`, and `physical-sciences-reference` stays a gap until a
  fixture-tested provider is classified in `packs/taxonomy.json`.
