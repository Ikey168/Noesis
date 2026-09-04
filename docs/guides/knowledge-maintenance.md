# Continuous knowledge maintenance

Noesis can continuously move enabled source-pack schedules through normalized
documents, the versioned Knowledge Engine workflow, selective derived-artifact
refresh, an append-only generation commit, and the unified query plane. The
worker is local-first: pinned fixtures are the default and live network access
requires an explicit flag.

## Bootstrap

Install and enable the desired source packs, accept each source's terms, and
create bounded interval schedules with the Knowledge Engine MCP tools. Then run
one foreground tick:

```bash
python scripts/knowledge_maintenance_worker.py --once
```

Run the deterministic six-domain conformance path without credentials or
network access:

```bash
make knowledge-maintenance-check
```

The reference covers all 6 packs and 22 sources, commits both successful and
degraded generations, verifies every receipt, and proves the committed
documents are queryable.

## Operation and scaling

Run `scripts/knowledge_maintenance_worker.py` without `--once` for a foreground
worker, or install `deploy/systemd/noesis-maintenance.service`. Configuration is
in `config/knowledge-maintenance.json`; credentials remain environment-backed
and are never stored there. Multiple workers may share the warehouse. Durable
leases, owner IDs, and monotonically increasing fencing tokens prevent two
workers from executing the same pack concurrently. Polling, catch-up, jobs per
tick, retries, and lease duration are bounded.

Live sources are opt-in:

```bash
python scripts/knowledge_maintenance_worker.py --live-network
```

Use separate worker processes for throughput. DuckDB still serializes writes,
while pack-level exclusion ensures a later interval never overtakes an active
one for the same pack.

## Recovery and visibility

Expired leases become retryable (or dead-letter after the attempt budget), and
unfinished source and workflow work resumes from stable run keys and receipts.
The worker advances a schedule only after a generation commits. Queries through
the maintained-document adapter consider only source runs referenced by a
committed end-to-end generation, so an interrupted workflow cannot leak a mixed
generation.

MCP read controls list due work and jobs, inspect attempts and generations,
verify replay, traverse lineage, and report health. Operator controls run one
job or a bounded drain. Administrative controls pause/resume schedules and
cancel or retry jobs. Audit records contain stable error classifications and
never raw credentials or quarantined payloads.

## Troubleshooting

- `credential_missing`, `license_not_accepted`, and `network_policy` are policy
  gates; correct operator configuration rather than bypassing them.
- `retry` means a bounded backoff remains. `dead-letter` means the attempt
  budget is exhausted and requires an explicit retry after fixing the cause.
- A degraded generation records optional-source omissions. Required-source
  failures do not commit a generation.
- Check `maintenance_health` for schedule/processing lag, stale leases, stuck
  workflow stages, stale sources, freshness, recovery time, and the last commit.
- Use `replay_maintenance_generation` before investigating data drift; it checks
  source receipts, workflow watermark/state, change event, and receipt hash.
