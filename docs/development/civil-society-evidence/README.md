# Civil society and nonprofits evidence (society.civil-society)

Evidence for the Society bundle's proposed `society.civil-society` provider
(subdomain `civil-society`, wave 2 tracker #2736; no per-track issue yet).

- [source-audit.md](source-audit.md) - CV01: per-source contract (IRS EO BMF,
  IRS Form 990 e-file, Charity Commission register, 360Giving Datastore),
  licence, access, rate limits, revision model, the data-minimisation decision
  for trustees, officers and grant recipients, and the bounded first coverage.
  Written without network access; terms not re-verified live.
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/civil_society_sources.py` exist; the acquisition issues add
  them and must match the audit.
- Live evidence: none yet. Until a dated live run exists every source is
  `unverified-live`, and offline coverage, once it exists, is never reported
  as live coverage.
