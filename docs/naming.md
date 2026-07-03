# Naming: NeuroNews to Noesis (R13 / Track N4)

The project began as NeuroNews, a news-analysis app, and grew into Noesis, a
general knowledge engine whose canvas is domain-neutral (news is one domain pack
among research, finance, legal, ...). R13 completes the rename of the
**user-facing** surfaces and documents which legacy identifiers survive, and
how.

The rule is **alias-first**: canonical names are `Noesis` / `NOESIS_*`, and the
old `NeuroNews` / `NEURONEWS_*` names remain supported so nothing external
breaks during the transition.

## Renamed (user-facing)

| Surface | Was | Now |
|---|---|---|
| Web package | `@neuronews/web` | `@noesis/web` |
| Web page title | `NeuroNews · Intelligence Terminal` | `Noesis · Intelligence Terminal` |
| API OpenAPI title | `NeuroNews API` | `Noesis API` |
| API root message | `NeuroNews API is running` | `Noesis API is running` |
| API description | "news articles and knowledge graph" | "the Noesis knowledge engine: documents, knowledge graph, analytics and the generative canvas" |

## Retained as documented aliases

These names are internal wiring or on-disk state where an external consumer,
existing deployment, or the full test suite could depend on the old value. They
survive as supported aliases; there is no user-facing surface that shows them.

| Identifier | Status | Why retained |
|---|---|---|
| `NEURONEWS_*` env vars | alias of `NOESIS_*` | Resolved by `src/config/env.py` (read `NOESIS_X`, else `NEURONEWS_X`). Set either; `NOESIS_*` is canonical. Covered by tests. |
| MCP server names `neuronews-*` (`.mcp.json` keys) | retained | The internal host runtime, discovery source labels, and every server test key on these. Renaming is a separate coordinated change; the names are not user-facing (an external host sees the outward `noesis` server, R13 #622). |
| Warehouse filename `data/neuronews.duckdb` | retained default | Existing warehouses on disk keep working; overridable via `NOESIS_DB_PATH` / `NEURONEWS_DB_PATH`. |
| `src/api/app_refactored.py`, module docstrings, auth/security module comments | retained | Internal identifiers and prose; not user-facing. |

## The env resolver

`src/config/env.py` is the one place the prefix fallback lives, so the whole
config surface aliases identically:

```python
from src.config.env import resolve_env, warehouse_path, enabled_packs

resolve_env("DB_PATH")     # NOESIS_DB_PATH, else NEURONEWS_DB_PATH
warehouse_path()           # the DuckDB path, same fallback, legacy default filename
enabled_packs()            # NOESIS_ENABLED_PACKS, else NEURONEWS_ENABLED_PACKS
```

Deprecation note: the `NEURONEWS_*` prefix is deprecated but supported. New
configuration should use `NOESIS_*`. A future major version may drop the legacy
prefix; until then both resolve identically (verified by
`tests/unit/config/test_env.py`).
