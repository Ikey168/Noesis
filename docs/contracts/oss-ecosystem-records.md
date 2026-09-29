# OSS Ecosystems record contracts

| Contract | JSON Schema | Owner |
| --- | --- | --- |
| `noesis-oss-ecosystem-record-v1` | [`contracts/schemas/jsonschema/noesis-oss-ecosystem-record-v1.json`](../../contracts/schemas/jsonschema/noesis-oss-ecosystem-record-v1.json) | `src/kb/oss_ecosystem_records.py`, stored by `src/kb/oss_ecosystem_store.py` |
| `noesis-oss-ecosystem-answer-v1` | [`contracts/schemas/jsonschema/noesis-oss-ecosystem-answer-v1.json`](../../contracts/schemas/jsonschema/noesis-oss-ecosystem-answer-v1.json) | `src/kb/oss_ecosystem_queries.py`, `src/kb/oss_ecosystem_graph.py` |

Both are registered in the schema registry (`src/kb/schema_registry.py`) by
`register_schemas` in `src/kb/oss_ecosystem_records.py` (MCP tool
`register_oss_schemas`). The schemas are kept with the other JSON Schemas under
`contracts/schemas/jsonschema/`; this page is their index.

Record types: `release_state_revision`, `declared_dependency_set`,
`licence_declaration_revision`, `publisher_organisation`,
`repository_link_assertion`, `archive_provenance`,
`published_dependency_graph`, `spdx_list_release`. Every record references the
Technology model's `package_object_id` / `immutable_artifact_id`; no record has
a field for an individual person (`FORBIDDEN_FIELDS`, asserted by
`tests/unit/oss_ecosystems/test_oss_records.py`).
