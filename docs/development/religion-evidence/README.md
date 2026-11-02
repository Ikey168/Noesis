# Religion and belief evidence (culture.religion)

Evidence for the proposed `culture.religion` provider of the `culture` bundle
([ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)) (subdomain
`religion-belief`, audit issue #2824, wave 3 tracker #2814; no per-track issue
yet).

- [source-audit.md](source-audit.md) - RB01: per-candidate contract for census
  religion tables (ONS Census 2021 via Nomis, Zensus 2022), Pew Research
  Center's religious composition data and ARDA; licence, access, the
  special-category minimisation decision (published aggregates only, publisher
  disclosure control kept, no person-level records), how question wordings and
  "no religion" categories stay side by side, and the bounded first coverage.
  Written without network access; terms not re-verified live. ONS and Zensus
  2022 (with a precondition on machine access) survive, `unverified-live`; Pew
  and ARDA are `not-implemented`.
- Offline evidence: none yet. No fixtures, provider or ingestion module exist.
- Live evidence: none yet.
