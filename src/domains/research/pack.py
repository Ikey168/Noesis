"""
Builds the research :class:`~src.domains.base.DomainPack` instance.

The research pack is the second first-class domain (R7 / Track N1), proving
pack-plurality: papers ingest with metadata enrichment (venue, citations,
concepts), and the canvas grows research panels (`citation_graph`, `venues`,
`literature_claims`) gated by the pack's ``ui_flags``. Its telemetry shifts
the empty canvas to research signal when the pack dominates.
"""

from src.domains.base import DomainPack, Enricher
from src.domains.research.enrichers import (
    citation_enricher,
    concept_enricher,
    venue_enricher,
)
from src.domains.research.telemetry import research_telemetry

_RESEARCH_ENRICHERS = [
    Enricher(
        name="venue",
        fn=venue_enricher,
        source_types=["paper"],
        description="Normalize the publication venue from paper metadata.",
    ),
    Enricher(
        name="citation",
        fn=citation_enricher,
        source_types=["paper"],
        description="Count references and incoming citations from metadata.",
    ),
    Enricher(
        name="concept",
        fn=concept_enricher,
        source_types=["paper"],
        description="Tag the paper's dominant concept from title/abstract.",
    ),
]

# UI feature flags surfaced to the frontend; gate the research panel family.
_RESEARCH_UI_FLAGS = {
    "research": True,
    "citation_graph": True,
    "venues": True,
    "literature_claims": True,
}

ResearchDomainPack = DomainPack(
    name="research",
    description=(
        "Research-domain analytics for scholarly papers: venue credibility, "
        "citation graphs, and literature claims. Runs only for "
        "source_type='paper' documents."
    ),
    source_types=["paper"],
    enrichers=_RESEARCH_ENRICHERS,
    route_modules=[],
    ui_flags=_RESEARCH_UI_FLAGS,
    telemetry=research_telemetry,
)
