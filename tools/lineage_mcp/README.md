# NeuroNews Lineage explorer (MCP server)

Wraps the **Marquez / OpenLineage** metadata service so *"if I change this
stage, what's downstream?"* is a typed tool instead of clicking the Marquez UI
or curling its API. The third question alongside the other two servers:

- `noesis-pipeline` — **run** a stage
- `noesis-contracts` — **is the boundary output valid**
- `noesis-lineage` — **what depends on it** (this one)

## Tools

| Tool | What it answers |
|---|---|
| `list_namespaces()` | Which OpenLineage namespaces exist |
| `list_nodes(namespace?, kind?)` | Dataset/job names to feed the other tools |
| `lineage(node, depth=2, namespace?)` | Compact lineage graph (nodes + edges) around a node |
| `impact(node, direction, namespace?)` | **Reachable set** — `downstream` = "what breaks if I change this", `upstream` = "what feeds this" |
| `run_history(job, namespace?, limit=5)` | Recent runs + states — did the stage run, did it fail |

A `node` is `dataset:<name>`, `job:<name>`, a full Marquez nodeId
(`dataset:<ns>:<name>`), or a bare name (tried as dataset then job).

## Config

| Var | Default |
|---|---|
| `MARQUEZ_URL` | `http://localhost:5000` |
| `MARQUEZ_NAMESPACE` | `neuronews` |

The server uses only stdlib (`urllib`) — needs just `fastmcp`. When Marquez is
unreachable, every tool returns a `{"error": …, "hint": …}` status (short
timeouts, never hangs).

## Bring up Marquez (required for live data)

The server queries a running Marquez. Start it from the repo root with the
committed override (which fixes three host-specific snags — see the override
file's header):

```bash
docker network create neuronews-network 2>/dev/null || true
docker compose -f docker-compose.lineage.yml \
    -f tools/lineage_mcp/marquez-local.compose.yml up -d marquez marquez-db
# wait for the API:
curl -s http://localhost:5000/api/v1/namespaces
```

Lineage is normally populated by the Airflow DAGs / Spark jobs that emit
OpenLineage (`jobs/spark/*_with_lineage.py`, `airflow/dags/news_pipeline.py`).
With an empty Marquez the tools work but return empty graphs.

## Register

In `.mcp.json` as `noesis-lineage`, launched from the repo root:

```bash
python3 tools/lineage_mcp/server.py
```

## Gotchas (hit while bringing Marquez up on Fedora/SELinux)

- **`neuronews-network` is declared `external`** — `docker network create
  neuronews-network` first or compose refuses to start.
- **`marquez-db` binds host `5432`**, which collides with other local
  Postgres containers. The override `!override`s its ports to drop the host
  binding; Marquez reaches the DB over the internal docker network.
- **SELinux blocks the bind mounts** (`Permission denied` on the mounted dir) —
  the override adds `:z` to relabel.
- **The repo's `marquez/config/marquez.yml` is incompatible with
  `marquez:latest`** — it uses the old `database:` key; `:latest` wants `db:`
  and rejects `cors`/`openlineage` blocks (`Unrecognized field at: database`).
  The override mounts `marquez.local.yml` (trimmed, current schema) instead.
