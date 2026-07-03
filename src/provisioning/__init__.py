"""
Provisioning plane (MCP rearchitecture plan, R8 / Track P).

Turns MCP from the read/compose plane into one that can *provision knowledge
domains*: an agent deploys a namespaced knowledge graph, binds the sources
that feed it (explicitly or by a quality criterion), routes matching
documents into the namespace, and the generative canvas grows a scoped panel
for it via R2 discovery. No code change, no deploy.

Modules:
  * :mod:`~src.provisioning.namespaces` - table-prefix namespacing
    (``kg_<name>_documents`` / ``_entities`` / ``_claims``) plus the routing
    that copies only matching rows out of the shared corpus. Shared tables are
    read, never written.
  * :mod:`~src.provisioning.store` - the registry (deployed KGs, bound
    sources) and the append-only lineage event log; every write is an
    idempotent upsert keyed by name.
  * :mod:`~src.provisioning.guardrails` - quotas, the deploy/teardown approval
    gate, and the ingest rate cap (the non-negotiable RW guardrails).
  * :mod:`~src.provisioning.provisioner` - the lifecycle orchestration
    (deploy / attach / ingest / status / list / teardown) the MCP tools call.

Stdlib-only; the caller injects the DuckDB connection (the API process owns
the single warehouse writer), so nothing here opens the warehouse itself.
"""

from src.provisioning.guardrails import GuardrailError, Quotas
from src.provisioning.provisioner import Provisioner

__all__ = ["Provisioner", "GuardrailError", "Quotas"]
