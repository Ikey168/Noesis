# Noesis schema registry MCP server

`noesis-schema-registry` manages runtime schemas, ontologies, constraints,
vocabularies, crosswalks, and reversible migrations. Definitions use immutable
semantic-version identities whose IDs include a SHA-256 content hash. Core
definitions resolve locally from the checkout even before registry tables have
been initialized.

The ontology alignment tools publish concepts as ontology modules and sourced
semantic mappings as dependent crosswalk modules. They add pinned object
validation and quarantine, bounded explainable query expansion, ontology diff,
and deterministic alignment export without introducing a second registry.

Read and validation are enabled by default. Every mutation is separately
operator-authorized through `NOESIS_MCP_SCOPES`:

| Scope | Operations |
|---|---|
| `knowledge:schema:read` | inspect, resolve, compare, impact, export, lineage, migration preview |
| `knowledge:schema:validate` | validate schema instances or ontology-typed knowledge objects; optionally quarantine failures |
| `knowledge:schema:register` | register modules, ontology concepts/crosswalks, and dependency edges |
| `knowledge:schema:deprecate` | deprecate a custom version |
| `knowledge:schema:migrate` | define, execute/resume, and compensate migrations |

`NOESIS_MCP_PRINCIPAL` must match the `actor.principal_id` on registrations and
migration definitions. Namespace migrations additionally require
`knowledge:namespace:<name>:read` for preview and `:write` for execution or
rollback.

The safe migration flow is preview, approve the returned content hash, execute
in bounded batches until `completed`, then inspect lineage and selective index
invalidations. Failed batches roll back to their last durable checkpoint.
Rollback creates compensating object revisions and preserves all module,
migration, checkpoint, change, and lineage records.
