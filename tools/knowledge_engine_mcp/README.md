# Knowledge Engine MCP

This server covers the final general-engine gaps: safe declarative REST/OpenAPI
manifests, immutable extractor versions, evidence-bearing canonical events, and
dependency-complete derived artifacts with selective checkpointed rebuilds.
Network secrets and executable extractor code are configured by operators and
never accepted as agent-returned knowledge.

It also exposes the Knowledge Engine 1.0 reference-workflow controls. Operators
can validate a seven-stage manifest, run or resume the canonical offline path,
inspect immutable receipts and committed watermarks, and read a stage from one
exact committed generation.

Production source-pack tools validate, install, upgrade, enable, disable, and
inspect the built-in research, political, economic, OSINT, technical, and
scientific connector configurations. Default conformance is fixture-backed and
network-free; live probes remain an explicit operator action.

The source-pack execution surface adds license and network preflight, durable
incremental and backfill runs, page checkpoints, quarantine retry, circuit
breakers, schedules, cancellation, receipts, replay, and domain coverage.
Execution uses pinned fixtures unless a caller explicitly enables live network
access.

Unified query tools expose authorized capability discovery, deterministic plan
explanation, bounded execution, cooperative cancellation, replay, and
evaluation. They compose local domain and temporal stores with optional memory
and federation. Memory remains context-only and never counts as evidence.

Continuous-maintenance tools turn due source-pack schedules into lease-safe,
fenced jobs; run one or drain a bounded batch; pause, resume, cancel, and retry;
and inspect status, health, generation receipts, deterministic replay, and full
lineage. Fixture-backed execution remains the default. Operator scope is needed
to execute work, while schedule and recovery controls require the dedicated
`knowledge:maintenance:admin` scope (or an operator acting as deployment admin).

For MCP clients that cannot expose this server's full tool catalog, launch a
separate instance with `NOESIS_MCP_TOOL_PROFILE=research`. It advertises only
the 16 intake and Deep Research tools needed to preflight, run, inspect, and
export a bounded research session; the default instance still advertises every
tool. Give the research instance `knowledge:intake:read/write` and the intended
`namespace:<name>:read/write` scopes. A project-backed session also needs
`knowledge:projects:read/write`. The profile narrows discovery only; each tool
still enforces its normal caller and namespace checks.
