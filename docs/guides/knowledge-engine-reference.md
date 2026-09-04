# Knowledge Engine 1.0 reference workflow

The reference workflow proves that Noesis's existing subsystems form one
coherent MCP-backed knowledge engine. It executes this canonical path:

```text
ingest -> extract -> resolve -> index -> query -> subscribe -> export
```

Run the deterministic, network-free fixture from a fresh checkout:

```bash
make knowledge-engine-reference
```

The command deliberately interrupts after extraction, resumes from the stored
stage receipts, commits a knowledge watermark only after the index is durable,
queries that exact generation, evaluates a saved subscription, exports a
portable namespace package, verifies its hashes, and prints a machine-readable
run report. The fixture spans research, political, economic, OSINT, technical,
and scientific records. It includes shared-origin reporting, independent
evidence, a contradiction, a correction, a retraction, a revised data vintage,
and a private stale memo.

Private and restricted components are omitted by the disclosure policy. Every
public query result carries a document/source/URL locator, and the export
contains the dependency closure and the query/subscription receipt.

Use a persistent database when inspecting recovery or replay behavior:

```bash
python scripts/knowledge_engine_reference.py \
  --database /tmp/noesis-reference.duckdb \
  --namespace reference \
  --run-key trial-1 \
  --exercise-recovery
```

Reusing the same manifest, run key, and input is idempotent. A changed run key
creates the next monotonic watermark. Operators can use the workflow MCP tools
to validate manifests, start the canonical workflow, inspect receipts, and
read a stage at an explicit committed watermark.

The workflow is an integration layer, not an alternate data plane. It calls
`DocumentStore`, `ExtractorRegistry`, `EventResolver`, `ArtifactGraph`,
`SubscriptionStore`, and `PortableNamespaceStore` through their public APIs.
