# Market operations, SLOs, and recovery

`MarketOperationsStore` is the repository-side operations boundary for market
research. It accepts bounded normalized observations and retains deterministic
receipts; it does not claim that a fixture observation is production telemetry.

The store covers:

- freshness, coverage, reconciliation, query latency, job success,
  notification delivery, throughput, bytes, and cost measurements;
- target-based SLO reports that return `pass`, `fail`, `degraded`, or `no_data`;
- provider health views, immutable request/cost budgets, and idempotent budget
  charges;
- four recovery scenarios: source outage, partial corruption, interrupted
  backfill, and restore;
- hash-verified bounded operations backups; and
- versioned repair runbooks and execution receipts.

The shared REST and MCP surfaces are exposed through the market capability
service. Each operation is namespace-authorized and uses one of
`market:operations:read`, `market:operations:write`, or
`market:operations:execute`. Missing telemetry is never silently converted to
healthy status, and backup restore returns an explicit limitation that the
deployment-specific database restore remains an operator step.

## Verification boundary

Unit fixtures can exercise all four failure scenarios, budget exhaustion,
idempotent charges, backup hash verification, SLO aggregation, and provider
health classification. `scripts/market_operations_benchmark.py` records a
bounded local ingestion/query capacity sample and a zero-provider-request cost
with infrastructure cost explicitly unmeasured. Production freshness,
concurrent capacity, latency, cost, backup
retention, and restore timings still require deployment telemetry and an
operator-run acceptance exercise. Those results belong in the operations issue
as separate live evidence rather than being inferred from these fixtures.
