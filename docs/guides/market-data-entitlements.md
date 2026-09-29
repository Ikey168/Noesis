# Market data entitlements and export checks

`MarketEntitlementStore` keeps namespace-scoped, append-only policy revisions keyed by provider entitlement. Each policy identifies its provider and license, active/revoked state, effective and optional expiry times, permitted capabilities, optional maximum retention age, the reviewer, and references to the rights evidence and decision. Credentials and full license documents do not belong in the policy row. Market source references continue to carry `provider`, `license_id` and `entitlement_id`; the store checks that these match the current policy.

## Permission decisions

Current policy is checked when a source is ingested, read, used for a market calculation, or attached to an as-of manifest. A registered policy is required even for operators; operator scope bypasses Noesis capability scopes but never overrides provider restrictions. Non-operator callers also need the matching `market:entitlement:<id>:<operation>` scope.

The enforced capability sets are:

| Operation | Required policy capabilities |
|---|---|
| Ingest | `ingest`, `retain` |
| Read | `read`, `retain` |
| Display | `read`, `display`, `retain` |
| Derive | `read`, `derive`, `retain` |
| Cache | `read`, `cache`, `retain` |
| Attach as evidence | `read`, `evidence`, `retain` |
| Internal export | `read`, `export`, `retain` |
| External export | `read`, `export`, `redistribute`, `retain` |

`max_retention_ms`, when present, is measured from the source reference's retrieval time. Reads and calculations are denied at the deadline. `purge_expired_source_revisions()` removes source revisions that the current policy no longer allows Noesis to retain. The purge is bounded per source table, reports table cursors when more rows remain, and stores only a Noesis object/revision ID, source hash, entitlement IDs and reason code in its audit tombstone. It does not copy the provider payload into the tombstone.

`MarketAsOfSnapshotStore.export_snapshot()` exports an immutable provenance manifest after checking current export rights. It includes revision and hash references, clocks, coverage and current rights decisions; it contains no OHLCV or filing-fact payload. `external=True` additionally requires redistribution rights.

`POST /api/v1/market/research/brief/evidence-bundle` and the
`noesis-market.export_market_brief_evidence_bundle` MCP capability use the
shared Noesis Evidence Bundle contract. They recheck the current `evidence`
policy when called; external bundles additionally require `export` and
`redistribute`. If any source is unverified, revoked, expired, or restricted,
the bundle is marked incomplete and contains only a rights-restriction receipt
and safe revision identifiers—no report sections, chart values, source locators,
or source payloads. Allowed bundles contain cited source metadata, not source
bytes.

## Example policy setup

```python
from src.domains.market.entitlements import MarketEntitlementStore

rights = MarketEntitlementStore(conn)
policy = rights.put_entitlement(
    "market:research",
    "provider-eod-license-2026",
    provider="provider-name",
    license_id="agreement-2026-01",
    capabilities=["ingest", "read", "retain", "derive", "evidence"],
    max_retention_ms=365 * 24 * 60 * 60 * 1000,
    evidence_ref="rights-review:document-id@revision-3",
    decision_ref="approval:market-license-2026-01",
    principal_id="rights-reviewer",
    scopes={"market:entitlements:admin", "namespace:market:research:write"},
)
```

The example is a configuration shape, not a claim that any provider grants these rights. Confirm vendor terms and populate each policy from reviewed evidence before ingesting licensed data. Missing, expired, revoked or mismatched policy fails closed.

## Integration boundary

This checkout enforces rights in the market instrument, price, corporate-action, financial-fact, quality, adjustment-calculation and as-of stores. Market brief and Evidence Bundle exports recheck current rights; internal brief output without complete entitlement metadata is marked `unverified`, while external output is withheld unless every source locator has a current matching entitlement. The Evidence Bundle path emits a content-free incomplete receipt when evidence or redistribution access is absent. Market REST and MCP adapters delegate to the same capability guard.

## Stored derived receipts are rechecked on read

Screener runs, quantitative and specialized runs, alert runs and research
artifacts persist values computed from licensed sources, so they behave as
caches. `recheck_stored_receipt_rights` evaluates *current* rights whenever one
is read. It walks the receipt for source revision IDs, resolves them to the
stored source refs (or to purge tombstones), and authorizes those refs for the
operation: `derive` on inspect, `export` on export.

| `current_rights.state` | Meaning |
| --- | --- |
| `authorized` | Every stored source currently permits the operation. |
| `partially_verified` | Authorized stored sources plus caller-supplied inputs that have no stored provenance. |
| `unverified` | Only caller-supplied inputs; no provider rights are asserted. |
| `withheld` | A source was purged, revoked or expired, or it does not permit the operation. |

A withheld read returns a content-free stand-in: `withheld: true`, the receipt
ID, the stored hash and the reason codes. It contains no results, input
snapshot or derived values. For example, revoking a policy after a screen ran
hides the cached screen, and removing `export` withholds only the export.
Research artifacts recheck their stored-source revisions only: their embedded
source locators are author citations, which the brief export rules above
already govern.

## Authored reports citing market evidence

`AuthoredReportStore.export` rechecks each cited dependency (`id`, `revision`
or `locator.revision_id`) that resolves to a stored market source revision, a
purged one, or a stored derived market receipt in the dependency's namespace.
Authorized exports carry `market_rights`. If any cited market evidence lacks
current export rights, the export fails with `market_rights_withheld` and the
reason codes. Reports without market evidence are unchanged.

## Retention: sources, then derived receipts

`MarketEntitlementStore.run_retention(namespace, ...)` runs one bounded pass.
First `purge_expired_source_revisions` purges source revisions whose policy no
longer permits retention. Then `purge_derived_receipts` deletes stored derived
receipts that depend on purged or no-longer-retainable sources. Each deletion
leaves a payload-free tombstone in `market_entitlement_purge_events`
(`object_kind` `derived:<kind>`). Receipts built only from caller-supplied
inputs are kept.

The maintenance worker (`scripts/knowledge_maintenance_worker.py`) schedules
this when its config contains:

```json
"market_retention": {"enabled": true, "namespaces": ["market:research"], "interval_s": 3600, "max_rows_per_table": 10000}
```

The worker runs retention as a deployment-owned operator once per interval
per namespace, and resumes a partial pass on the next tick. It reports its
readiness at start-up and prints a `noesis-market-retention-tick-v1` line when
work ran.

Provider-specific commercial rights and samples remain pending in
[#1655](https://github.com/Ikey168/Noesis/issues/1655). No vendor policy is
populated in this checkout.
