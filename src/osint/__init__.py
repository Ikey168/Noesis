"""
OSINT composition plane (MCP rearchitecture plan, R10 / Track OSINT phase 1).

Defensive, analytical primitives over already-ingested public documents. Pure
composition of the layers Noesis already builds (M6 claims, the RAG
evidence-link table, the semantic conflict table, and outlet/source scores):
nothing here crawls, targets, or de-anonymizes anyone.

Modules:
  * :mod:`~src.osint.corroboration` - ``corroborate(claim_id)``: how many
    *independent* sources support or contradict a claim, weighted by source
    credibility. Never collapses to a single confidence number.
  * :mod:`~src.osint.reliability` - ``source_reliability(source)``: the outlet
    transparency scoring generalized to any ``source_type``, plus a
    corroboration hit-rate and correction history.
  * :mod:`~src.osint.contradictions` - ``contradiction_scan(entity|topic)``:
    where the public record disagrees with itself, from the CONTRADICTS edges,
    every entry cited (uncited flagged, never hidden).

Stdlib-only; the caller injects a read-only DuckDB connection.
"""

from src.osint.contradictions import contradiction_scan
from src.osint.corroboration import corroborate
from src.osint.dossier import entity_dossier
from src.osint.investigations import (
    GATED_TOOLS,
    investigation_audit,
    is_gated,
    list_investigations,
    osint_telemetry,
)
from src.osint.paths import relationship_path
from src.osint.reliability import source_reliability
from src.osint.timeline import timeline_reconstruct

__all__ = [
    # R10 composition
    "corroborate",
    "source_reliability",
    "contradiction_scan",
    # R11 investigation surface
    "entity_dossier",
    "relationship_path",
    "timeline_reconstruct",
    "investigation_audit",
    "list_investigations",
    "osint_telemetry",
    "GATED_TOOLS",
    "is_gated",
]
