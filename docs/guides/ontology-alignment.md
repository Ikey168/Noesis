# Ontology alignment

Ontology alignment builds on Noesis's existing schema registry. Every ontology
and semantic crosswalk is an immutable, content-addressed schema module, so
publication, semantic-version resolution, dependency checking, compatibility,
deprecation, audit lineage, and deterministic export retain one source of
truth.

Ontology content defines stable concept identifiers, multilingual labels,
definitions, broader relationships, constraints, lifecycle, namespace URI,
generation, valid time, observation time, producer, and policy. Publication
rejects duplicate identifiers, unknown parents, hierarchy cycles, malformed
versions, and attempts to replace a published version. Deprecation changes
lifecycle state without changing published content.

Crosswalk modules relate source and target versions using `equivalent`,
`broader`, `narrower`, `related`, or `incompatible` mappings. Mappings retain
confidence, evidence, conditions, and explicit local-extension status.
Many-to-many and conflicting mappings coexist; an incompatibility blocks query
expansion over the disputed pair instead of silently choosing one assertion.

Validation pins an exact ontology module and supports entities, relations,
events, metrics, and claims. Required-field and type errors are bounded and the
source-native representation is always retained. Callers may quarantine an
invalid partial record idempotently for later repair. This makes constraint
drift visible when the same object is checked against a later ontology version.

Query expansion is explicit and bounded by relationship types, depth, and term
count. Results rank the identity concept first, then hierarchy and crosswalk
paths by deterministic penalties and mapping confidence. Each term includes
its pinned module version and explainable path. Ambiguous top-ranked mappings,
conflicts, and truncation are reported.

The schema-registry MCP server exposes ontology publication, inspection,
deprecation, crosswalks, validation and quarantine, expansion, version diff,
and deterministic export. These operations use the established
`knowledge:schema:read`, `knowledge:schema:register`,
`knowledge:schema:validate`, and `knowledge:schema:deprecate` scopes.
