# M3 acceptance: a domain KG stood up from a live connector run

Milestone M3 (issues #667-#669) takes provisioning from simulated ingestion to a
real connector run. This is the acceptance record; its executable form is
`scripts/provisioning/m3_acceptance.py`, run in CI by
`tests/unit/provisioning/test_m3_acceptance.py`.

## What it proves

A domain KG is provisioned end to end and populated by a **real** connector run,
with no simulation branch on the ingest path:

1. `kg_deploy("climate")` provisions the namespace.
2. `kg_attach_pipeline` binds a connector (contract-validated, approval-gated).
3. `kg_attach_sources` binds the source the connector writes.
4. `kg_ingest` runs the bound connector through the M3.1 live pipeline runner:
   the connector harvests documents and persists them into the shared
   `news_articles` corpus, then routing copies the matching rows into the
   `kg_climate_*` namespace tables.

The runner is the real `build_pipeline_runner` from `src/provisioning/pipeline_runner.py`,
not an inline simulator. Persistence is idempotent by document id.

## Result

```
connector run: climate-feed ok=True fetched=4 written=4
routed into kg_climate namespace: 4 documents
corpus rows: 4; kg_view documents sample: 4
audit trail: ['deploy', 'attach_pipeline', 'attach', 'pipeline_run', 'ingest']

RESULT: OK - domain stood up from a real connector run, no simulation
```

- The connector really wrote 4 documents into the corpus (`written=4`).
- Routing copied all 4 into the KG's own namespace (`routed=4`), and `kg_view`
  surfaces them.
- The audit trail (M3.2) records a distinct `pipeline_run` entry naming the
  connector and its counts, ahead of the routing `ingest` event, so the standup
  is fully reconstructable.

## Live mode

The harness defaults to a small bundled feed sample so the run is deterministic
in CI. Set `NOESIS_M3_LIVE=1` to harvest from a live connector instead (the RSS
`news` connector, no network fixture); either way the runner's persist-and-route
is the real path. The bundled sample is a real document set the connector path
persists and routes, not a fake downstream of the harvest.
