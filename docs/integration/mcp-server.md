# Noesis MCP gateway

Noesis has one **default MCP endpoint** for agents and interactive clients:

```text
http://127.0.0.1:8100/mcp
```

Start it with:

```bash
noesis serve
```

The default server is `tools/noesis_mcp/server.py` (`noesis`). It intentionally
exposes a compact daily-driver surface instead of every internal capability:

- `domains`
- `add`
- `search`
- `ask`
- `brief`
- `documents`
- `claims`
- `inspect_source`
- `coverage`
- `watch`
- `inbox`
- `explore`
- `research`
- `export`

The gateway routes into the existing KB, production ingestion, Information
Intake, and evidence-bundle implementations; it does not maintain a second copy
of those semantics. Its intake workflow tools act as the configured workspace
principal. Use the specialist Knowledge Engine MCP for per-caller identities
and granular multi-tenant intake scopes.

## Specialist servers

The existing `tools/*_mcp/server.py` servers remain available for advanced or
least-privilege deployments. For example:

```bash
python tools/statistics_mcp/server.py
python tools/knowledge_engine_mcp/server.py
noesis serve --surface kb-mcp
```

They are not required for the ordinary Noesis workflow and are not mounted into
the default gateway automatically.

## Transports

`noesis serve` defaults to Streamable HTTP on loopback. For a local spawned
process, stdio is also available:

```bash
noesis serve --transport stdio
```

For remote access, bind deliberately and configure authentication:

```bash
NOESIS_MCP_AUTH_TOKEN='replace-me' \
noesis serve --host 0.0.0.0 --port 8110
```

The resulting endpoint is `http://HOST:PORT/mcp`. Authentication remains
fail-closed when a token is configured. Do not expose an unauthenticated MCP
server beyond loopback.

The specialist KB contract remains `noesis-kb-v1`; the gateway uses
`noesis-gateway-v1` only for gateway-specific envelopes and errors.
