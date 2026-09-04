# Federation MCP server

`noesis-federation` exposes bounded read-only access to approved local and
external knowledge sources. SQL access accepts typed select/aggregate requests,
never SQL text. Python integrations can register SQL, vector, graph, remote MCP,
or contract-test fake adapters through `src.kb.federation`.

Every result retains its source-native score semantics, timestamp and
provenance. Multi-source failures produce partial coverage rather than a false
complete answer. Grant `knowledge:federation:read`; namespace-specific vector
and graph reads additionally require `namespace:<name>:read`.
