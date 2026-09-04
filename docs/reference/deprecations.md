# Public compatibility and deprecation policy

Noesis uses `noesis-*` for MCP server registrations and `NOESIS_*` for its
environment variables. Compatibility aliases are accepted through Noesis 1.x,
emit `DeprecationWarning` when used, and have a removal target of Noesis 2.0,
not before 2027-09-01. Removal requires a release-note entry and a major-version
change; aliases are never removed silently.

## MCP server aliases

| Deprecated | Canonical |
|---|---|
| `neuronews-pipeline` | `noesis-pipeline` |
| `neuronews-contracts` | `noesis-contracts` |
| `neuronews-lineage` | `noesis-lineage` |
| `neuronews-blog-feeds` | `noesis-blog-feeds` |
| `neuronews-domain-packs` | `noesis-domain-packs` |
| `neuronews-research` | `noesis-research` |
| `neuronews-provisioning` | `noesis-provisioning` |
| `neuronews-osint` | `noesis-osint` |
| `neuronews-dataset` | `noesis-dataset` |
| `neuronews-statistics` | `noesis-statistics` |
| `neuronews-arguments` | `noesis-arguments` |
| `neuronews-kg` | `noesis-kg` |
| `neuronews-sources` | `noesis-sources` |
| `neuronews-security` | `noesis-security` |
| `neuronews-monitoring` | `noesis-monitoring` |

The mapping is machine-readable as `compatibilityAliases` in `.mcp.json`.
Calls through the supervised host are resolved to the canonical server, so
legacy and canonical calls share sessions and caches.

## Environment aliases

For every Noesis-owned variable, `NOESIS_<NAME>` is canonical and
`NEURONEWS_<NAME>` is the deprecated fallback. If both are present, the
canonical value wins and no legacy warning is emitted. This applies to the MCP
transport variables as well as database, pack, logging, security, and local
runtime settings.
