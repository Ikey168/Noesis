# Social protection evidence (society.social-protection)

Evidence for the Society bundle's proposed `society.social-protection` provider
(subdomain `social-protection`, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - SS01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage for Eurostat ESSPROS, OECD SOCX and ILOSTAT SDG 1.3.1.
  Written without network access; terms not re-verified live.
- Offline evidence: authored fixtures (reference years 2094-2098, releases
  2098-2099, synthetic values) replay through the real `social-protection`
  adapter: `tests/fixtures/source_packs/society-*.json` (first releases, pinned
  in `config/source_packs/society-social-protection.json`) and
  `tests/fixtures/social_protection/*_revision.json` (revisions, a restating
  report edition, an OECD estimate year replaced and a removal);
  `tests/unit/domains/test_social_protection_records.py` and
  `tests/unit/domains/test_social_protection_sources.py` (SS02-SS05), identity,
  links, queries, monitoring and MCP tests (SS06-SS11), and the offline
  acceptance journey `tests/unit/domains/test_social_protection_acceptance.py`
  (SS12: runtime replay with sockets blocked, a failed run, revisions, an OECD
  estimate year, a restated ILO edition, function review, Demographics and
  COFOG links, a country with no records). Guide:
  [society-social-protection.md](../../guides/society-social-protection.md).
  Offline status: covered offline, not live.
- Live evidence: none yet. Until a dated live run exists every source is
  `unverified-live` (the ILO dashboards `not-implemented`) and offline coverage
  is never reported as live coverage.
