# Research recipes

Research recipes are immutable declarative DAGs over known MCP tools. Each
revision declares typed public and secret inputs, step dependencies, tool and
contract compatibility, capability scopes, source terms, network policy,
output classification, budgets, and named outputs. Canonical hashing makes
ordering and schema upgrades inspectable; cycles and unknown tools are rejected.

The local runner persists a checkpoint after every completed step. A crashed
run resumes without repeating durable work, retries are bounded, optional-step
failures become explicit omissions, and cancellation is observed between
checkpoints. Runs pin the exact recipe, public inputs, secret references,
snapshot tokens, and tool versions. Expired snapshots fail closed and replay
reports tool-version or output-hash drift.

Secret values are resolved only at execution and are redacted from adapter
state, logs, checkpoints, receipts, and exports. A preview must satisfy each
step's scopes, source terms, network gate, and pinned tool version before work
starts. MCP uses `knowledge:recipes:read`, `knowledge:recipes:write`, and
`knowledge:recipes:execute`; registry lists are paginated and every operation
is namespace-isolated.

The MCP run endpoint accepts bounded local step outputs as adapters, which also
provides deterministic offline conformance across research, political,
economic, OSINT, technical, and scientific workflows.
