# Integrating Noesis via MCP + API

Noesis is a **capability plane** with one default MCP gateway, specialist MCP
servers, and a REST API. It has no bespoke UI — you drive it from your own
client, an agent host (Claude Desktop, another agent), or another service. Most
clients should connect only to the default `noesis` gateway; specialist servers
remain available for advanced workflows.

## MCP servers

The default agent-facing server is `tools/noesis_mcp/server.py`. It exposes a
small daily-driver surface: `domains`, `add`, `search`, `ask`, `brief`,
`documents`, `claims`, `inspect_source`, `coverage`, `watch`, `inbox`,
`explore`, `research`, and `export`. Start it with:

```bash
noesis serve
# Streamable HTTP: http://127.0.0.1:8100/mcp
```

Every subsystem also remains available as a specialist
[FastMCP](https://github.com/jlowin/fastmcp) server under
`tools/<name>_mcp/server.py`, declared in [`.mcp.json`](../../.mcp.json).
Specialist servers can run standalone over stdio:

```bash
python tools/statistics_mcp/server.py     # one server, stdio transport
```

| Server | Module | What it exposes |
|---|---|---|
| `noesis` | `tools/noesis_mcp` | Default curated gateway: add/search/ask/brief/source inspection/watches/inbox/exploration/research/export |
| `noesis-pipeline` | `tools/pipeline_mcp` | Documents, articles, trending, clusters, sentiment; figures, corrections, geo, speaker balance |
| `noesis-arguments` | `tools/argument_mcp` | Claims, stances, frames, positions, outlet scoring/clustering |
| `noesis-kg` | `tools/kg_mcp` | Knowledge-graph entities, relations, communities, centrality |
| `noesis-kb` | `tools/kb_mcp` | Unified KB contract, briefs, diffs, claims, evidence, and integrity verification |
| `noesis-osint` | `tools/osint_mcp` | Corroboration, reliability, contradiction ledger, dossiers, paths, timelines, image provenance/reuse |
| `noesis-statistics` | `tools/statistics_mcp` | Statistical series, claim-vs-data checks, the data-check ledger |
| `noesis-research` | `tools/research_mcp` | Venues, citation graph, literature claims |
| `noesis-provisioning` | `tools/provisioning_mcp` | Deploy/attach/ingest/teardown namespaced knowledge graphs |
| `noesis-sources` | `tools/sources_mcp` | Source comparison, trustworthiness, profiles |
| `noesis-domain-packs` | `tools/domain_packs_mcp` | Domain-pack status and install |
| `noesis-blog-feeds` | `tools/blog_mcp` | Blog/RSS watchlists and digests |
| `noesis-dataset` | `tools/dataset_mcp` | Argument-mining dataset inspector |
| `noesis-lineage` | `tools/lineage_mcp` | OpenLineage run/dataset lineage |
| `noesis-contracts` | `tools/contract_mcp` | Data-contract schemas and validation |
| `noesis-monitoring` | `tools/monitoring_mcp` | Pipeline health and metrics |
| `noesis-security` | `tools/security_mcp` | Security posture and checks |
| `noesis-schema` | `tools/schema_mcp` | Warehouse schema introspection |
| `noesis-catalog` | `tools/catalog_mcp` | Least-privilege capability, domain, pack, transport, and readiness discovery |
| `noesis-transactions` | `tools/transactions_mcp` | Authorized dry-run, atomic mutation commit, audit replay, and compensating rollback |
| `noesis-schema-registry` | `tools/schema_registry_mcp` | Versioned definitions, validation, crosswalks, compatibility, impact, and reversible migrations |
| `noesis-federation` | `tools/federation_mcp` | Read-only external-source discovery, typed queries, merging, and coverage evaluation |
| `noesis-subscriptions` | `tools/subscriptions_mcp` | Saved-query lifecycle, replayable change polling, quotas, and delivery outbox |
| `noesis-namespaces` | `tools/namespaces_mcp` | Deterministic namespace packages, verification, conflict previews, and atomic import |
| `noesis-memory` | `tools/memory_mcp` | Typed scoped memory, explained retrieval, lifecycle, corrections, and standard MCP mapping |
| `noesis-knowledge-engine` | `tools/knowledge_engine_mcp` | Declarative API manifests, extractor versions, event resolution, artifact lineage, and rebuilds |

### Connecting an external host

Point any MCP client at the server command. For Claude Desktop, add to its
config:

```jsonc
{
  "mcpServers": {
    "noesis": {
      "command": "python",
      "args": ["tools/noesis_mcp/server.py"],
      "env": { "NOESIS_CONFIG": "/path/to/.noesis/config.json" }
    }
  }
}
```

The full set is in [`.mcp.json`](../../.mcp.json). Start with the single
`noesis` entry; add specialist entries only when a workflow needs their
advanced tools.

### Capability discovery

The default gateway has a fixed, intentionally small tool list, so ordinary
clients do not need a separate discovery server. For advanced workflows that
attach specialist servers, call `noesis-catalog.capability_catalog` before
planning. It is generated from
the registered FastMCP tools and includes their input/output schemas, scopes,
mutability, cost/latency classes, required data, and readiness. Its public MCP
surface omits operator mutations, disabled or empty capabilities, and private
domain/namespace metadata. Operators can regenerate the complete diagnostic
artifact with:

```bash
python scripts/generate_mcp_catalog.py
python scripts/generate_mcp_catalog.py --check
```

The artifact is `contracts/generated/noesis-mcp-catalog-v1.json`; its contract
is `contracts/schemas/jsonschema/noesis-mcp-catalog-v1.json`.

Legacy `neuronews-*` server names and `NEURONEWS_*` environment variables are
warning-emitting aliases. See [the deprecation policy](../reference/deprecations.md).

### Remote access (Streamable HTTP + auth)

stdio works when the consuming project can spawn the server process locally.
For anything else — another machine, a container, a hosted agent — every
server also runs over **Streamable HTTP**, opt-in via env vars
(`src/mcp_host/transport.py`):

```bash
NOESIS_MCP_AUTH_TOKEN=your-shared-secret \
noesis serve --host 0.0.0.0 --port 8110
```

- Raw specialist server scripts still default to stdio for spawned-process
  setups. `noesis serve` defaults to the curated gateway over Streamable HTTP.
- The default gateway needs only one endpoint: `http://127.0.0.1:8100/mcp`.
  Specialist servers may still use separate ports when explicitly deployed.
  The default bind is `127.0.0.1`, so exposure beyond localhost is deliberate.
- **Auth is fail-closed.** With `NOESIS_MCP_AUTH_TOKEN` set, every HTTP
  request must present the token as a Bearer credential; if the installed
  fastmcp offers no supported token verifier, the server **refuses to start**
  rather than silently serving unauthenticated. Unset means open — intended
  only for the localhost default.

The default gateway's `inbox`, `explore`, and `research` tools operate as the
workspace principal from `.noesis/config.json`, which is appropriate for the
single-user/local daily-driver endpoint. For caller-scoped or multi-tenant
Information Intake sessions, use the specialist `noesis-knowledge-engine`
server and set `NOESIS_MCP_AUTH_TOKENS_FILE` instead of the shared token. The file is a
private (mode `0600`) JSON object whose keys are 32-character-or-longer bearer
tokens and whose values contain a unique `client_id` and a nonempty `scopes`
list. For example, an intake writer needs `knowledge:intake:write` and
`namespace:research:write`; reads need the corresponding read scopes. The
[intake mode guide](../subsystems/intake-modes.md) describes the session
contract and remaining integration limits. Do not configure both token env
variables.

An HTTP client entry then looks like:

```jsonc
{
  "mcpServers": {
    "noesis": {
      "url": "http://noesis-host:8110/mcp",
      "headers": { "Authorization": "Bearer your-shared-secret" }
    }
  }
}
```

### Knowledge transactions

Knowledge writes use `noesis-transactions` and a preview-bound approval flow.
The server validates the mutation contract, provisioned namespace ontology,
evidence, permissions, and expected revisions without changing knowledge
state. A commit must provide the unchanged envelope and returned
`approval_hash`; it then applies the batch atomically and records provenance,
invalidations, a consolidation watermark, and an append-only audit event.
Retries are idempotent. Rollbacks are new compensating revisions, not history
deletion. See the [operator guide](../../tools/transactions_mcp/README.md) for
the permission scopes and workflow.

### Runtime schema registry

`noesis-schema-registry` resolves content-addressed schemas, ontologies,
constraints, vocabularies, and crosswalks without a network dependency. Core
definitions are built in; custom semantic versions are immutable. Registration
checks compatibility policy before writing, and impact analysis follows
declared lineage into connectors, extractors, indexes, tools, packs, dependent
modules, and stored object groups.

Migrations use a separate preview and write permission path. They execute in
checkpointed batches, validate preconditions and target postconditions,
preserve object identity and provenance, selectively invalidate derived
artifacts, and support audited compensating rollback. See the
[schema registry operator guide](../../tools/schema_registry_mcp/README.md).

### Discipline the servers follow

- **Read tools are isolated.** Read-only servers open DuckDB read-only and
  degrade to an empty (still valid) payload when a table is missing. The
  transaction server is the explicit exception: it uses atomic writes guarded
  by operator-configured scopes.
- **Statistical-honesty contract.** Analytic tools return `n` / `method` /
  `assumptions` and an interval on any headline figure (`src/analytics/honesty.py`).
- **Evidence discipline.** OSINT tools cite every rendered line; uncited items
  are flagged, never hidden (`src/osint/evidence.py`).
- **Review gate.** Sensitive OSINT tools (`geolocate_claims`,
  `narrative_coordination`, and the imagery external tier) stay off unless
  `NOESIS_OSINT_GATED_TOOLS` is on. See
  [osint-review-gate.md](../security/osint-review-gate.md).

## REST API

The same subsystems are reachable over HTTP for clients that prefer REST
(`src/api/`). Run it with:

```bash
NOESIS_DEV_MODE=true NOESIS_DB_PATH=/tmp/noesis-dev.duckdb \
  uvicorn src.api.app:app --port 8012
```

Route families live in `src/api/routes/` — documents, argument mining, the
knowledge graph, evidence/veracity, search, sentiment, reports, sources,
provisioning, agents, and more. Each is registered behind a feature-flag import
in `src/api/app.py`, so an unavailable optional dependency degrades gracefully
rather than failing startup. Browse `/docs` (OpenAPI) on the running server for
the live route list.

### Auth

- `NOESIS_DEV_MODE=true` disables the WAF for local development.
- In production, the API enforces WAF + rate limiting; API-key and RBAC routes
  are under `src/api/routes/` (`api_key_routes`, `rbac_routes`, `auth_routes`).
  See [security.md](../security/overview.md).

## Example: check a quantitative claim against data

Over MCP (`noesis-statistics`), or the equivalent Python:

```python
import duckdb
from src.argument_mining.quantities import QuantityExtractor
from src.analytics.claim_check import check_assertion

conn = duckdb.connect("noesis.duckdb", read_only=True)
assertion = QuantityExtractor().extract("Unemployment in Germany rose in 2024.")[0]
result = check_assertion(conn, assertion)
# -> {"verdict": "supported", "n": 2, "method": "...", "assumptions": [...],
#     "observed": {"value": 3.4, "lo": ..., "hi": ...}, "series_id": "wb:...:DE"}
```

The `verdict` is always three-valued (`supported` / `contradicted` /
`unverifiable`) — a claim that resolves to no matching series is `unverifiable`,
never a guess.

## Agents

An audited agent host (`src/agent/`) drives Noesis over two planes —
**provisioning** and **osint** — through a runtime that budgets tool calls,
enforces a per-plane allowlist, respects the review gate, and records a
replayable transcript. Agents can dispatch in-process (`build_local_caller`) or
against the live MCP host (`live_caller`). See the analyst and investigator
agents for end-to-end examples.
