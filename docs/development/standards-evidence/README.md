# Standards and certification evidence (technology.standards)

Evidence for the Technology bundle's `technology.standards` provider (subdomain
`standards-certification`, ISO source `iso-open-data`, adapter
`src/ingestion/standards_sources.py`, store `src/kb/standards.py`).

- [depth-audit.md](depth-audit.md) - DS01 (#2828, wave 4 tracker #2825):
  second-publisher audit under ADR-006. Confirms that only catalogue metadata
  and links are stored, never standard text. Recommends the W3C Technical
  Reports API (RFC Editor index as the alternative) after a provider widening of
  `noesis-standard-catalogue-v1`; NANDO (`not-implemented`: needs a new record
  shape and a decision record first) and ETSI (`not-implemented`: no documented
  machine interface). Written without network access (2026-11-01); terms marked
  _verify_, no source live.
- Offline evidence: `tests/fixtures/source_packs/standards-iso-open-data.json`.
  Live evidence: none yet.
