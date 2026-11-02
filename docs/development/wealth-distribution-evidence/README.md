# Wealth distribution evidence (society.wealth)

Evidence for the Society bundle's `society.wealth` provider (issue #2882;
taxonomy subdomain `income-poverty-inequality`, no new subdomain).

- [source-audit.md](source-audit.md) - WD01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage for the World Inequality Database, the ECB
  Distributional Wealth Accounts and the Federal Reserve Distributional
  Financial Accounts; the ECB HFCS statistical tables and the Bundesbank PHF
  aggregates are `not-implemented` (no machine-readable aggregates known).
  Written without access to the publishers; terms not verified live.
- Offline evidence: authored fixtures (reference periods 2093-2098, releases
  2098-2099, synthetic values) replay through the real `wealth-distribution`
  adapter: `tests/fixtures/source_packs/society-wealth-*.json` (first
  releases, pinned in `config/source_packs/society-wealth-distribution.json`)
  and `tests/fixtures/wealth_distribution/*_revision.json` (revisions, a WID
  method change, a WID extrapolated year replaced, a DWA estimate confirmed and
  a removal); `tests/unit/domains/test_wealth_distribution_sources.py`,
  `test_wealth_distribution_queries.py`, `test_wealth_distribution_mcp.py` and
  the offline acceptance journey `test_wealth_distribution_acceptance.py`
  (runtime replay with sockets blocked, a failed run, vintages, as-of
  selection, sources side by side, `comparability_unknown`). Guide:
  [society-wealth.md](../../guides/society-wealth.md). Offline status: covered
  offline, not live.
- Live evidence: none yet. Until a dated live run exists every implemented
  source is `unverified-live` and offline coverage is never reported as live
  coverage.
