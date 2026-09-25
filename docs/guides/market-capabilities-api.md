# Market capability API

The market domain exposes one shared Python service through authenticated REST,
MCP, and the generated capability catalog. The service returns the same JSON
envelope from both transports: successful calls use `{"ok": true, "result": …}`;
domain failures use `{"ok": false, "error": {"code", "message", "details?"}}`.

Every read takes a namespace and explicit acquisition/publication cutoffs where
the underlying source supports them. The adapter derives the principal and
scopes from the validated JWT or the MCP server's trusted `NOESIS_MCP_PRINCIPAL` and
`NOESIS_MCP_SCOPES` environment. Callers cannot pass an identity or scope in tool or
request arguments. Domain stores recheck namespace membership, record ownership,
document access, and current provider entitlements for each operation. Provider
entitlement scopes are data-dependent; a read needs the current source's
`market:entitlement:<id>:read` grant and a calculation needs its
`market:entitlement:<id>:derive` grant, in addition to the static capability
scope and `namespace:<name>:read`.

REST endpoints are mounted under `/api/v1/market`:

| Operation | Endpoint |
| --- | --- |
| Read local source coverage | `GET /readiness?namespace=…` |
| Resolve a ticker, identifier, or exact object revision | `GET /instruments/lookup` |
| Read end-of-day bars | `GET /prices/history` |
| Read filing facts/statements | `GET /financial-statements` |
| Read a saved macro snapshot | `GET /economic-snapshots/{snapshot_id}` |
| Calculate price and benchmark metrics | `POST /calculations/prices` |
| Calculate filing-fact ratios | `POST /calculations/facts` |
| Generate, export, or deliver a market brief | `POST /research/brief`, `/research/brief/export`, `/research/brief/deliver` |
| Export a rights-checked Evidence Bundle | `POST /research/brief/evidence-bundle` |

For example, with a JWT whose `sub` is `analyst:acme` and whose signed claims
include `market:prices:read` and `namespace:research:read`:

```sh
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/api/v1/market/prices/history?namespace=research&listing_id=listing%3Aexample&start_ms=1767573000000&end_ms=1768005000000&acquired_by_ms=1768005000000&publicly_available_by_ms=1768005000000&page_size=100'
```

The `noesis-market` MCP server offers matching tools named
`market_readiness`, `lookup_market_instrument`, `get_market_price_history`,
`get_market_financial_statements`, `inspect_economic_release_snapshot`,
`calculate_market_price_metrics`, and `calculate_market_fact_metrics`. A local
MCP server can be scoped with `NOESIS_MCP_PRINCIPAL=analyst:acme` and
`NOESIS_MCP_SCOPES=market:prices:read,namespace:research:read`. Do not put those values
in individual tool arguments.

The matching brief tools include `generate_market_brief`, `export_market_brief`,
and `export_market_brief_evidence_bundle`. Evidence Bundle export evaluates
current source rights on each call; if any source is restricted, the shared
Evidence Bundle contract contains a content-free incomplete receipt. External
bundles additionally require redistribution rights.

Price history pages scan at most 1,000 source candidates per request and expose
an integer `next_cursor`; a page can be shorter when ownership or current rights
checks hide source rows. Filing-fact pages use the same rule and are bounded at
100,000 candidates. `scan_bound_reached` makes a hard scan limit explicit.
Macro snapshots contain at most 50 series per page. The daily price calculation
and fact calculation limits are enforced by the underlying domain stores, and
calculation receipts preserve their selected source revisions and formula
versions for replay.

`market_readiness` reports namespace-level retained-store states and explicitly
reports live provider adapters as `not_configured`. Local fixture records are
not production coverage, market completeness, or provider permission. The
readiness result does not substitute for the per-record authorization performed
when data is read. REST requests that disconnect do not currently interrupt an
in-flight synchronous calculation; calculations remain bounded and their ledger
receipt is durable once committed.
