# Noesis knowledge transactions MCP server

`noesis-transactions` exposes controlled writes to generic knowledge objects
and relations. Every write follows the same flow:

1. Call `preview_mutation_batch` with a `noesis-knowledge-mutation-v1` envelope.
2. Review its deterministic creates, updates, links, retractions, conflicts,
   warnings, and downstream invalidations.
3. Pass the returned `approval_hash` and unchanged envelope to
   `commit_mutation_batch`.
4. Use `replay_mutation_audit` to reconstruct commits and compensating
   rollbacks. Rollback creates new compensating revisions; it never deletes
   audit history.

Preview is side-effect free for knowledge state. It validates the JSON
contract, evidence, actor/provenance pairing, namespace access, expected
revisions, relation endpoints, and any `object_types` / `relation_types`
allowlists stored in the provisioned namespace ontology.

Identity and permissions come from operator-controlled environment settings,
never from tool arguments:

| Variable | Default | Purpose |
|---|---|---|
| `NOESIS_MCP_PRINCIPAL` | `local-operator` | Principal; must match `envelope.actor.principal_id` |
| `NOESIS_MCP_SCOPES` | preview + read only | Comma-separated transaction and namespace scopes |
| `NOESIS_DB_PATH` | local warehouse | Transaction store |

Scopes are independent: `knowledge:transaction:preview`,
`knowledge:transaction:commit`, `knowledge:transaction:rollback`, and
`knowledge:transaction:read`. Non-corpus namespaces additionally require
`knowledge:namespace:<name>:read` for preview or `:write` for commit/rollback.

Commit and rollback are disabled by default; an operator must explicitly add
their scopes before MCP write tools can change state.
