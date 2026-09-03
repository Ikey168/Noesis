# Noesis MCP servers

There is no monolithic or phantom `noesis_mcp` server. The public knowledge
contract is `tools/kb_mcp/server.py`; specialist capabilities are separate
least-privilege servers listed in [mcp-and-api.md](mcp-and-api.md) and declared
in the repository's `.mcp.json`.

Run the KB server over local stdio:

```bash
python tools/kb_mcp/server.py
```

It exposes domain discovery, scoped search/documents/claims/entities, diffs,
coverage, integrity envelopes, and the daily brief through `noesis-kb-v1`.
Its additive cross-domain tools search, answer, and inspect equivalence links
over explicit or all-authorized domain scopes through
`noesis-cross-domain-v1`; private domains remain grant-gated.
All successful responses carry the versioned contract envelope. Query-only KB
and KG servers open the configured DuckDB file read-only when they run as a
standalone process.

For Streamable HTTP, opt in explicitly:

```bash
NOESIS_MCP_TRANSPORT=http \
NOESIS_MCP_HTTP_HOST=127.0.0.1 \
NOESIS_MCP_HTTP_PORT=8100 \
NOESIS_MCP_AUTH_TOKEN='replace-me' \
python tools/kb_mcp/server.py
```

stdio remains the default. Do not bind outside loopback without authentication,
TLS termination, and network access controls. Each specialist server has its
own process and port; there is no implicit all-powerful gateway.
