# M4 acceptance: two tenants, isolated and concurrent

Milestone M4 (issues #670-#673) takes provisioning from single-operator to
isolated multi-tenant. This is the acceptance record; its executable form is
`scripts/provisioning/m4_acceptance.py`, run in CI by
`tests/unit/provisioning/test_m4_acceptance.py`.

## What it proves

Two tenants (`acme`, `globex`) run their full provisioning lifecycles interleaved
on one shared warehouse. The API owns a single writer, so "concurrent" here means
interleaved operations, not parallel writes.

1. **Isolation.** Each tenant lists and acts on only its own namespaces.
2. **No cross-tenant access.** `acme.status("globex_markets")` is `not_found`;
   `globex.ingest("acme_energy")` is refused. The other tenant's KG is invisible.
3. **Per-tenant quotas.** With `NOESIS_PROV_MAX_KGS=1`, each tenant deploys its
   one KG (neither blocks the other), and a second KG for either is refused with
   `quota_max_kgs` (M4.2).
4. **Routing isolation.** Each tenant's live ingest (the M3.1 runner) routes only
   its own source's documents into its own namespace.
5. **Reconstructable.** Each tenant is replayable from its own audit trail.

## Result

```
acme sees ['acme_energy']; globex sees ['globex_markets']
cross-tenant read refused: not_found; cross-tenant write refused: not_deployed
per-tenant quota: acme second deploy quota_max_kgs, globex second deploy quota_max_kgs
routed documents: acme=3, globex=3
acme trail: ['deploy', 'attach_pipeline', 'attach', 'pipeline_run', 'ingest']
globex trail: ['deploy', 'attach_pipeline', 'attach', 'pipeline_run', 'ingest']

RESULT: OK - two tenants isolated, concurrent, with independent quotas
```

## Backends

The harness uses the default table-prefix backend for reproducibility. The same
isolation holds on the attached-DuckDB (P2) and external-Postgres (M4.3)
backends, where each KG additionally has its own database; a per-tenant DSN
(`NOESIS_PROV_PG_DSN_<TENANT>`) gives each tenant its own Postgres.
