"""Pack and workflow composition (docs/architecture/pack-workflow-composition.md).

Contracts (C02), a pure resolver (C03), catalog explanations (C04), durable
lifecycle state (C05), source integration (C06) and plan-bound workflow
dispatch (C07). Composition state lives in composition-metadata tables on the
existing warehouse connection (ADR-003); it never duplicates source, ontology
or session stores.
"""
