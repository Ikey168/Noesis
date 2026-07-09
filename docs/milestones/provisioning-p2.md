# Provisioning phase 2 acceptance: databases and pipelines (Track P2)

R8/R9 made the provisioning MCP orchestrate the knowledge-graph slice: deploy a
namespaced KG (a table prefix in the shared warehouse), bind already-ingested
sources, route their documents in, tear down. Track P2 (issues #640-#644)
extends it to orchestrate **everything**: a KG can deploy into its own database,
bind and run its own pipeline, and the whole domain reconstructs from its audit
trail.

The executable form of this report is
[`scripts/provisioning/p2_acceptance.py`](../../scripts/provisioning/p2_acceptance.py);
the regression is
[`tests/unit/provisioning/test_p2_backends_pipelines.py`](../../tests/unit/provisioning/test_p2_backends_pipelines.py).

## What changed

- **Isolation backends (#640).** A KG's `backend` is `table-prefix` (default,
  R8) or `attached`. An `attached` KG gets its own DuckDB file, attached under
  the alias `kg_<name>`, so its documents/entities/claims live in a separate
  database entirely (`kg_<name>.documents`, ...), not `kg_<name>_documents` in
  the shared warehouse. Routing reads the shared corpus and writes across into
  the KG's database. Teardown detaches the database and leaves the file on disk.
- **`kg_attach_pipeline` (#641).** Binds a connector or feed to a KG,
  contract-validated at attach (a feed with no url, or a config that fails its
  ingest contract, is refused), idempotent by `(kg, connector)`, recorded in
  lineage.
- **Orchestrated `kg_ingest` (#642).** With bound pipelines and a runner, each
  connector runs first (connector to contract to enrich), then routing copies
  the matching documents into the namespace; per-pipeline progress is in the
  ingest lineage event. With no runner it degrades to routing already-ingested
  documents (R8 behaviour).
- **Guardrails (#643).** New quotas: max provisioned databases and max
  pipelines per KG. Deploying a database backend and attaching a pipeline are
  approval-gated. Teardown detaches the database and unbinds pipelines, and
  never cascades to the shared corpus. Each has a failing-path test.

## Result (live run)

```
Standing up two domains, each with its own database and pipeline:
  [energy]  deploy(attached) -> attach_pipeline -> attach_source -> ingest: pipeline ran (3 ingested), routed 3 docs into its own database
  [markets] deploy(attached) -> attach_pipeline -> attach_source -> ingest: pipeline ran (2 ingested), routed 2 docs into its own database
isolation: kg_energy.documents=3, kg_markets.documents=2, separate databases ['kg_energy', 'kg_markets']
  [energy]  audit trail: ['deploy', 'attach_pipeline', 'attach', 'ingest']
  [markets] audit trail: ['deploy', 'attach_pipeline', 'attach', 'ingest']
teardown energy: database detached, file kept
RESULT: OK - two domains live via provisioning alone, each with its own database and pipeline
```

- **Own database.** Each KG's rows live in its own attached DuckDB
  (`kg_energy`, `kg_markets`), separate databases in `duckdb_databases()`, not
  a prefix in the shared warehouse.
- **Own pipeline.** The connector ran on ingest (writing fresh rows into the
  shared corpus, as a real connector to contract to enrich step would), then
  routing copied the matching rows into the KG's database, the full connector
  to route path.
- **Reconstructable.** Each domain replays from its lineage: deploy,
  attach_pipeline, attach, ingest.
- **Clean teardown.** Detaches the database (file kept, never deleted) and
  unbinds the pipeline; the shared corpus is untouched.

The same flow runs over the real `provisioning_mcp` server:
`kg_deploy(backend="attached")`, `kg_attach_pipeline` (preview then approve, a
bad config refused with `contract_invalid`), `kg_ingest`, `kg_status`
(reporting `backend` and `pipeline_count`), and `kg_teardown` (detaching the
database and the pipeline).

## Answer to "does it orchestrate everything?"

Yes, now: databases (own DuckDB per KG), knowledge graphs (R8 namespacing), and
pipelines (bind a connector, run it on ingest, route the results), all from the
one guardrailed, audited provisioning surface.

Note on the live connector: in this harness the pipeline runner simulates a
connector by writing to the shared corpus, so the run-to-route path is proven
without a live external feed. Wiring the runner to the real `pipeline_mcp`
connector execution (so `kg_ingest` pulls from an actual RSS/document source) is
the one remaining integration point; the orchestration, guardrails and audit
around it are complete and tested.
