"""
Domain-pack validation vocabulary.

The neutral home for the small set of constants ``pack_format`` validates a pack
manifest against. These used to live in ``src/genui`` (the generative UI), which
has been retired in favour of consuming Noesis purely through its MCP servers
and REST API. A pack's ``facets`` / ``default_span`` / ``planner_keywords`` are
now advisory metadata (there is no canvas that renders them), but manifests
still declare them, so validation keeps accepting the known vocabulary rather
than breaking shipped packs.
"""

from __future__ import annotations

from typing import Tuple

from services.ingest.common.document_model import SOURCE_TYPES  # canonical

# Facet vocabulary a pack manifest may reference (advisory since the UI retired).
FACETS: Tuple[str, ...] = (
    "overview",
    "trend",
    "sentiment",
    "claims",
    "stance",
    "actors",
    "conflict",
    "sources",
    "entities",
    "events",
    "library",
)

# Layout span bounds a pack panel's default_span was validated against.
MIN_SPAN = 3
MAX_SPAN = 12

__all__ = ["FACETS", "MIN_SPAN", "MAX_SPAN", "SOURCE_TYPES"]
