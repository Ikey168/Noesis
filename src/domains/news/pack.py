"""
Builds the news :class:`~src.domains.base.DomainPack` instance.

Kept in its own module so that ``src.domains.news.__init__`` stays trivial
and tests can import the pack object directly without triggering registration.
"""

from src.domains.base import DomainPack, Enricher
from src.domains.news.telemetry import news_telemetry
from src.domains.news.enrichers import (
    event_clusterer_enricher,
    fake_news_enricher,
    influence_enricher,
    sentiment_enricher,
)

_NEWS_ENRICHERS = [
    Enricher(
        name="fake_news_detector",
        fn=fake_news_enricher,
        source_types=["news"],
        description="Trustworthiness scoring via RoBERTa/DeBERTa (FakeNewsDetector).",
    ),
    Enricher(
        name="sentiment_analysis",
        fn=sentiment_enricher,
        source_types=["news"],
        description="Sentiment label + score via the NLP sentiment analyzer.",
    ),
    Enricher(
        name="event_clusterer",
        fn=event_clusterer_enricher,
        source_types=["news"],
        description="Tags documents as eligible for event-cluster batch processing.",
    ),
    Enricher(
        name="influence_network_analyzer",
        fn=influence_enricher,
        source_types=["news"],
        description="Source influence score from the influence-network graph.",
    ),
]

# Route modules whose .router should be mounted when the news pack is enabled.
_NEWS_ROUTE_MODULES = [
    "src.api.routes.news_routes",
    "src.api.routes.article_routes",
    "src.api.routes.sentiment_routes",
    "src.api.routes.sentiment_trends_routes",
    "src.api.routes.event_routes",
    "src.api.routes.event_timeline_routes",
    "src.api.routes.veracity_routes",
    "src.api.routes.influence_routes",
]

# UI feature flags surfaced to the frontend.
_NEWS_UI_FLAGS = {
    "sentiment_dashboard": True,
    "timeline": True,
    "clusters": True,
    "trending": True,
    "watchlists": True,
    "influence_graph": True,
}

NewsDomainPack = DomainPack(
    name="news",
    description=(
        "News-domain analytics: fake-news detection, sentiment analysis, "
        "event clustering, and influence-network analysis. "
        "Runs only for source_type='news' documents."
    ),
    source_types=["news"],
    enrichers=_NEWS_ENRICHERS,
    route_modules=_NEWS_ROUTE_MODULES,
    ui_flags=_NEWS_UI_FLAGS,
    telemetry=news_telemetry,
)
