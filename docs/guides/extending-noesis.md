# Extending Noesis

Add a connector when the input is a new source format, a domain when existing
documents need a durable scope, and an MCP tool only when a capability cannot
be expressed through `noesis-kb-v1`.

1. Normalize sources to `document-ingest-v1` and store through
   `DocumentStore`; do not create a parallel document table.
2. Register the connector in `src/ingestion/connectors/__init__.py` and add a
   deterministic offline fixture.
3. Put domain membership in `config/domains.yml`; avoid query-time hidden
   filters. A domain may use the corpus view or a provisioned namespace, but
   consumers keep the same KB contract.
4. Route generated claims through the argument-mining ledger and record
   `prediction_mode`, confidence, and evidence locators.
5. For analytical tools, build and validate the
   [honesty envelope](../contracts/honesty-envelope.md). For OSINT tools, add
   an explicit review gate and a refusal path before enabling external access.
6. Give every MCP tool a description, typed input schema, structured result or
   stable error, and a direct harness test. Query-only servers must open DuckDB
   read-only.

Agents are useful when a task requires a bounded sequence across several tools
(for example provision → ingest → compare → brief) and the replay/audit trail is
valuable. A direct deterministic function is preferable for a single query.
Agents do not relax source, uncertainty, budget, permission, or review rules.
