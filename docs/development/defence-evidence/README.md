# Defence and security evidence (political.defence)

Evidence for the Political bundle's proposed `political.defence` provider
(subdomain `defence-security`, issue #2816, wave 3 tracker #2814; no per-track
tracker yet).

- [source-audit.md](source-audit.md) - DF01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage. Written without network access (publisher hosts
  blocked, 2026-11-01); terms not re-verified live. NATO defence expenditure
  tables and UN Register of Conventional Arms national reports are
  `unverified-live`, each conditional on a machine-readable file; the SIPRI
  Military Expenditure and Arms Transfers databases are `not-implemented`
  (terms understood to restrict redistribution, _verify_).
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/defence_sources.py` exist.
- Live evidence: none. Until a dated live run exists every kept source is
  `unverified-live`, and `defence-security` stays a gap until a fixture-tested
  provider is classified in `packs/taxonomy.json`.
