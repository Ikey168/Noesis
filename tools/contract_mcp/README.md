# NeuroNews Data-contract server (MCP server)

Wraps `contracts/` so the **governed boundary between pipeline stages** is a
typed tool, not a grep. When you touch a stage, the bug-catching question is
*"does the output still satisfy the contract the next stage expects?"* — this
answers it with a compact pass/fail + drift report. Pairs directly with the
connector-scaffold workflow: scaffold a connector, then `validate("ingest", …)`
its output.

## Tools

| Tool | What it does |
|---|---|
| `list_contracts()` | List available contracts (JSON Schema + Avro), their `id`, title, and the stage aliases that resolve to each — so you pick a stage without grepping. |
| `get_contract(stage)` | The contract a stage's output must satisfy: compact field summary (required vs optional, types, `additionalProperties`) + the raw JSON Schema + its Avro counterpart path. |
| `validate(stage, sample)` | Pass/fail + **drift report** against the stage's contract(s). |

`stage` accepts a contract id (`article-ingest-v1`) or a friendly alias
(`ingest`, `connector`, `scrape`, `ask`, `answer`, `sentiment`, …).

`sample` accepts an inline object, a path to a JSON file, or a named example
under `contracts/examples/` (e.g. `valid-full-article`, `invalid-missing-url`).

## Dual-schema validation (the important part)

Stages backed by both a JSON Schema and an Avro schema are checked against
**both**, and the result carries an `agree` flag. A disagreement is itself a
contract bug. This server found a live one on its first run:

- `contracts/schemas/jsonschema/article-ingest-v1.json` types `published_at` /
  `ingested_at` as **string** (ISO-8601 date-time).
- `contracts/schemas/avro/article-ingest-v1.avsc` types them as **long**
  (`timestamp-millis`, epoch integers).
- The golden `contracts/examples/valid/*.json` fixtures (and the running
  pipeline, which validates against Avro) use epoch integers.

So `validate("ingest", "valid-full-article")` returns `valid: false`,
`agree: false` — jsonschema rejects the epoch ints, avro accepts them. The
inverse (ISO-string sample) flips it. The JSON Schema and Avro contracts for
`article-ingest-v1` need reconciling.

The Avro check is **structural** (field presence, no-unknowns, broad type
compatibility) — it does not do binary/logical-type encoding validation, so no
`avro` runtime dependency is required.

## Run / register

Registered in `.mcp.json` as `noesis-contracts`, launched from the repo root:

```bash
python3 tools/contract_mcp/server.py
```

Requires `fastmcp` and `jsonschema` (`pip install -r tools/contract_mcp/requirements.txt`).

## Design

- **Lazy imports**, repo-root-relative, summaries not payloads (errors capped at
  `MAX_ERRORS`).
- **Contract registry** auto-discovered by scanning
  `contracts/schemas/jsonschema/**.json` and `contracts/schemas/avro/**.avsc` —
  new contracts appear automatically; only the friendly stage aliases are
  hand-maintained in `STAGE_ALIASES` / `AVRO_ALIASES`.
