# Market operations fixture acceptance

This is repository evidence for the local portion of issue #1693. It is kept
separate from production telemetry and must not be read as a provider SLA.

## Delivery state (audited 2026-09-26)

- Shipped: Fixture evidence only, as stated above; no provider SLA is claimed.
- Composition dependency: none.

| Check | Fixture result |
| --- | --- |
| Query latency observations | 10 ms and 20 ms; p95 target evaluation observed 20 ms |
| Coverage observation | 0.80, explicitly `degraded` when the observation is marked degraded |
| Cost observation | 12 cost-micros for the bounded fixture operation |
| Budget control | 2-request/100-cost-micros budget; duplicate charge replay is idempotent and over-limit charge is rejected |
| Recovery scenarios | `source_outage`, `partial_corruption`, `interrupted_backfill`, and `restore` are represented by typed drill receipts |
| Backup integrity | content hash is checked before restore; mismatched hashes fail closed |
| Audit retention | dry-run and cutoff-based deletion cover high-volume measurement and charge-ledger rows while retaining recovery evidence |
| Local capacity | `scripts/market_operations_benchmark.py` measures bounded single-process telemetry ingestion and provider-health query latency; the dated JSON receipt is retained in `config/market/acceptance_packs/local-operations-benchmark.json` |
| Local provider cost | 0 requests / 0 cost-micros because the benchmark is local; infrastructure cost is explicitly `not_measured` |

The unit suite exercises these checks in
`tests/unit/domains/test_market_operations.py` and
`tests/unit/test_market_operations_benchmark.py`. Production capacity,
provider freshness, concurrent query latency, notification delivery,
backup/restore timing, infrastructure cost and licensed-provider request cost
still require a configured deployment. They remain explicit gaps and are not
inferred from the local receipt.
