# Archives and historical records evidence (culture.archives)

Evidence for the proposed `culture.archives` provider of the `culture` bundle
([ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)) (subdomain
`archives-genealogy`, audit issue #2823, wave 3 tracker #2814; no per-track
issue yet).

- [source-audit.md](source-audit.md) - AR01: per-candidate contract for
  Archivportal-D (through the existing DDB connector), Archives Portal Europe and
  the US National Archives Catalog API; licence, access, the personal-data bound
  (finding-aid levels above the file, no person-level indexes, a 110-year
  cut-off for any later file-level amendment) and the bounded first coverage.
  Written without network access; terms not re-verified live. Archivportal-D
  survives, `unverified-live`; Archives Portal Europe and NARA are
  `gated-not-granted` (no keys held).
- Offline evidence: none yet. No fixtures, provider or ingestion module exist.
- Live evidence: none yet.
