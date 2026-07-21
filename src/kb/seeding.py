"""
Idempotent feed seeding: make the subscription store match the domain config.

``config/domains.yml`` declares which feeds each domain wants harvested;
``config/blog_subscriptions.json`` (the SubscriptionStore) is what the blog
connector actually reads. Seeding reconciles the two without clobbering user
edits: missing feeds are subscribed under the union of the feed's and the
domain's tags, and existing subscriptions only ever *gain* tags — names,
extra user tags, and user-added feeds are preserved.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.kb.registry import KnowledgeDomainRegistry


def seed_domain_feeds(
    registry: Optional[KnowledgeDomainRegistry] = None,
    store: Optional[Any] = None,
) -> Dict[str, Any]:
    """Subscribe every domain feed that is not already subscribed.

    Returns a summary: feeds added, feeds whose tags were extended, and
    feeds already up to date.
    """
    from src.ingestion.connectors.blog.subscriptions import SubscriptionStore
    from src.kb.registry import load_registry

    registry = registry or load_registry()
    store = store or SubscriptionStore()

    summary: Dict[str, Any] = {"added": [], "retagged": [], "unchanged": []}
    for definition in registry.domains():
        for feed in definition.feeds:
            wanted_tags = sorted({*feed.tags, *definition.tags})
            existing = store.get(feed.url)
            if existing is None:
                store.subscribe(feed.url, name=feed.name, tags=wanted_tags)
                summary["added"].append(feed.url)
                continue
            merged = sorted({*existing.tags, *wanted_tags})
            if merged != sorted(existing.tags):
                store.subscribe(
                    feed.url, name=existing.name or feed.name, tags=merged
                )
                summary["retagged"].append(feed.url)
            else:
                summary["unchanged"].append(feed.url)
    return summary
