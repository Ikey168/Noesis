"""
Panel catalog — the single source of truth for what the generative UI can
render.

Every entry maps a panel type to the backend endpoint that feeds it, the
warehouse tables it needs, the domain-pack ``ui_flag`` that gates it, the
intent facets it serves, and layout defaults. The frontend keeps a renderer
registry keyed by the same ``type`` strings (apps/web/src/genui/registry.tsx);
the two must stay in sync — the smoke test asserts the catalog is exposed via
``GET /api/v1/ui/panels`` so the frontend can introspect it.

Stdlib-only on purpose: importing this module must never fail, otherwise the
route registration in src/api/app.py silently disables the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PanelDef:
    """Static definition of one renderable panel type."""

    type: str
    title: str
    description: str
    endpoint: Optional[str]
    facets: Tuple[str, ...]
    tables: Tuple[str, ...] = ()
    ui_flag: Optional[str] = None
    default_span: int = 6
    topic_param: Optional[str] = None
    source_type_param: Optional[str] = None
    days_param: Optional[str] = None
    # Upper bound of the endpoint's `days` Query validator, so generated
    # params never draw a 422 from the endpoint they target.
    max_days: Optional[int] = None


# Facets an intent can express. The planner scores each facet from keyword
# evidence and selects panels serving the highest-scoring facets.
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

PANEL_CATALOG: Tuple[PanelDef, ...] = (
    PanelDef(
        type="note",
        title="Plan",
        description="Narrative note explaining how the canvas was assembled.",
        endpoint=None,
        facets=FACETS,
        default_span=12,
    ),
    PanelDef(
        type="kpi_row",
        title="Signal summary",
        description="Headline counts across articles, clusters and topics.",
        endpoint="/api/v1/news/articles",
        facets=("overview",),
        tables=("news_articles",),
        default_span=12,
    ),
    PanelDef(
        type="articles",
        title="Latest documents",
        description="Most recent matching articles and documents.",
        endpoint="/api/v1/news/articles",
        facets=("overview", "sentiment"),
        tables=("news_articles",),
        default_span=6,
    ),
    PanelDef(
        type="documents",
        title="Library",
        description="Ingested documents across all source types (books, papers, transcripts, …).",
        endpoint="/api/v1/documents",
        facets=("library", "overview"),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="trending",
        title="Trending topics",
        description="Topics ranked by mention velocity.",
        endpoint="/topics/trending",
        facets=("overview", "trend", "events"),
        tables=("news_articles",),
        ui_flag="trending",
        default_span=6,
        days_param="days",
        max_days=30,
    ),
    PanelDef(
        type="clusters",
        title="Event clusters",
        description="Grouped event coverage with velocity and impact.",
        endpoint="/api/v1/events/clusters",
        facets=("overview", "events"),
        tables=("news_articles",),
        ui_flag="clusters",
        default_span=6,
    ),
    PanelDef(
        type="watchlists",
        title="Watchlist",
        description="Tracked entities and topics with mention velocity and alerts.",
        endpoint=None,
        facets=("events", "trend"),
        ui_flag="watchlists",
        default_span=6,
    ),
    PanelDef(
        type="timeline",
        title="Story timeline",
        description="Chronological development of a tracked story.",
        endpoint=None,
        facets=("events",),
        ui_flag="timeline",
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="sentiment_heatmap",
        title="Sentiment heatmap",
        description="Topic × time sentiment intensity grid.",
        endpoint="/news_sentiment/heatmap",
        facets=("sentiment", "trend"),
        tables=("news_articles",),
        ui_flag="sentiment_dashboard",
        default_span=6,
        days_param="days",
        max_days=60,
    ),
    PanelDef(
        type="topic_sentiment",
        title="Sentiment by topic",
        description="Average sentiment score per topic.",
        endpoint="/news_sentiment/topics",
        facets=("sentiment",),
        tables=("news_articles",),
        ui_flag="sentiment_dashboard",
        default_span=6,
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="entity_graph",
        title="Entity graph",
        description="Co-mention network of entities in recent coverage.",
        endpoint="/api/v1/entity_graph",
        facets=("entities", "actors", "overview"),
        tables=("news_articles",),
        ui_flag="influence_graph",
        default_span=6,
        days_param="days",
        max_days=30,
    ),
    PanelDef(
        type="claims",
        title="Extracted claims",
        description="Claims mined from documents with fact-check verdicts.",
        endpoint="/api/v1/arguments/claims",
        facets=("claims", "conflict"),
        tables=("argument_claims",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="stance",
        title="Stance breakdown",
        description="Supportive / critical / neutral stance mix per topic.",
        endpoint="/api/v1/arguments/stance",
        facets=("stance", "conflict", "sentiment"),
        tables=("source_stances",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="frames",
        title="Framing by source",
        description="How each outlet frames the story (economic, legal, …).",
        endpoint="/api/v1/arguments/frames/source",
        facets=("sources", "claims"),
        tables=("document_frames",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="positions",
        title="Actor positions",
        description="Policy positions held by actors, with updates over time.",
        endpoint="/api/v1/arguments/positions",
        facets=("actors", "stance"),
        tables=("policy_positions",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="controversy",
        title="Conflicts",
        description="Actor pairs with contradicting claims, by intensity.",
        endpoint="/api/v1/arguments/controversy",
        facets=("conflict", "claims"),
        tables=("claim_conflicts",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="drift",
        title="Stance drift",
        description="Detected stance reversals and shifts per source.",
        endpoint="/api/v1/arguments/stance/drift",
        facets=("trend", "stance"),
        tables=("stance_drift_events",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="outlet_ranking",
        title="Outlet transparency ranking",
        description="Outlets scored by framing diversity, attribution, neutrality.",
        endpoint="/api/v1/arguments/outlets/ranking",
        facets=("sources",),
        tables=("outlet_scores",),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="outlet_clusters",
        title="Outlet clusters",
        description="Outlets grouped by editorial framing (PCA scatter).",
        endpoint="/api/v1/arguments/outlets/clusters",
        facets=("sources", "entities"),
        tables=("outlet_clusters",),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="actors",
        title="Key actors",
        description="Most-mentioned speakers, subjects and authors.",
        endpoint="/api/v1/arguments/actors/summary",
        facets=("actors", "entities"),
        tables=("document_actors",),
        default_span=6,
        source_type_param="source_type",
    ),
)

PANEL_TYPES: Tuple[str, ...] = tuple(p.type for p in PANEL_CATALOG)

_BY_TYPE: Dict[str, PanelDef] = {p.type: p for p in PANEL_CATALOG}


def get_panel_def(panel_type: str) -> Optional[PanelDef]:
    """Return the catalog entry for a panel type, or None."""
    return _BY_TYPE.get(panel_type)


def panel_catalog_dict() -> List[Dict[str, Any]]:
    """JSON-serializable catalog for the /api/v1/ui/panels endpoint."""
    return [
        {
            "type": p.type,
            "title": p.title,
            "description": p.description,
            "endpoint": p.endpoint,
            "facets": list(p.facets),
            "tables": list(p.tables),
            "ui_flag": p.ui_flag,
            "default_span": p.default_span,
            "topic_param": p.topic_param,
            "source_type_param": p.source_type_param,
            "days_param": p.days_param,
            "max_days": p.max_days,
        }
        for p in PANEL_CATALOG
    ]
