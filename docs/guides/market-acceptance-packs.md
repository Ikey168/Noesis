# Market acceptance packs

`config/market/acceptance_packs/fixture-pack.json` records the checks that are
actually executable in this repository. The companion live-source template
defines the evidence required before adding an exchange, asset class, provider,
or intraday entitlement.

The capability matrix deliberately separates fixture verification from live
coverage and licensing. The current live-source index points to verified SEC
filing and FRED macro receipts plus an incomplete FMP price-provider receipt.
A provider adapter may be present while its credential scope or commercial
rights are insufficient; readiness and export paths preserve that state.
Migration notes should identify contract revisions, source revision mappings,
historical replay changes, and any changed formula or entitlement policy.

The provider-neutral asset-class fixture engines and their limits are described
in [specialized market analytics](market-specialized-analytics.md). Their
passing fixture checks extend the fixture pack, but do not satisfy the live
source, independent pricing, capacity, or analyst-review rows.

`config/market/acceptance_packs/asset-class-index.json` is the compact status
index for every current expansion. It lists the same six required gates for
each asset class, links the retained evidence and guide, and records migration
notes. Its top-level status remains `partial` while any live-source,
entitlement or user-workflow gate is blocked.
