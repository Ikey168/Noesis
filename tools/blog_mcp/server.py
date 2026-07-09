"""
NeuroNews Blog-feeds MCP server.

Developer tool for managing RSS/Atom feed subscriptions, running watchlists,
and triggering ingestion from the Claude interface without touching the
filesystem directly.

Tools
-----
list_subscriptions()
    All subscribed feeds with their name, tags, and subscription date.

subscribe_feed(url, name?, tags?)
    Add (or update) a feed subscription. Persists to
    ``config/blog_subscriptions.json``.

unsubscribe_feed(url)
    Remove a subscription by URL.

feed_status()
    Summary: subscription count, source_type registered in connector
    registry, whether readability-lxml is available.

ingest_feeds(limit_per_feed?, fetch_full_text?, extract_entities?,
             feed_urls?)
    Trigger a live ingestion run — fetch latest posts from all subscriptions
    (or a subset via feed_urls) and optionally run entity extraction.
    Returns a run summary; does NOT write to a database.

run_watchlist(keywords, tag_filter?, limit_per_feed?, fetch_full_text?)
    Fetch latest posts from subscriptions and immediately run keyword
    matching against them.  Returns matched posts with which keywords
    triggered the match.

harvest_feed(url, name?, limit?)
    Fetch and parse a single feed URL (without persisting a subscription)
    and return a post summary.  Good for previewing a feed before
    subscribing.

Design constraints (same as other NeuroNews MCP servers):
  * Lazy imports inside every tool body — server starts in <100 ms.
  * Summaries, not payloads — content fields are previewed at 300 chars.
  * ``config/blog_subscriptions.json`` is the authoritative store for
    subscriptions; each mutating tool reads then writes it atomically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SUBS_PATH = REPO_ROOT / "config" / "blog_subscriptions.json"

mcp = FastMCP("neuronews-blog-feeds")

CONTENT_PREVIEW = 300


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _store():
    from src.ingestion.connectors.blog.subscriptions import SubscriptionStore
    return SubscriptionStore(SUBS_PATH)



def _preview(text: Optional[str]) -> str:
    if not text:
        return ""
    if len(text) > CONTENT_PREVIEW:
        return text[:CONTENT_PREVIEW] + f"… [{len(text)} chars]"
    return text


def _doc_summary(doc) -> dict:
    return {
        "document_id": doc.document_id,
        "title": doc.title or "",
        "url": doc.url or "",
        "source_id": doc.source_id or "",
        "authors": list(doc.authors or []),
        "created_at": doc.created_at,
        "content_preview": _preview(doc.content),
        "has_full_text": bool((doc.metadata or {}).get("has_full_text")),
        "tags": list((doc.metadata or {}).get("tags", [])),
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool
def list_subscriptions() -> dict:
    """List all subscribed RSS/Atom feeds.

    Returns:
        count         - number of subscriptions
        subscriptions - list of {url, name, tags, added_at}
    """
    subs = _store().list()
    return {
        "count": len(subs),
        "subscriptions": [
            {
                "url": s.url,
                "name": s.name,
                "tags": s.tags,
                "added_at": s.added_at,
            }
            for s in subs
        ],
    }


@mcp.tool
def subscribe_feed(url: str, name: str = "", tags: str = "") -> dict:
    """Add or update a feed subscription.

    Persists to ``config/blog_subscriptions.json``.

    Args:
        url:  Full RSS/Atom feed URL.
        name: Human-readable label (defaults to the URL).
        tags: Comma-separated topic tags, e.g. ``"tech,research"``.
              Used by watchlists to scope keyword matching.

    Returns:
        subscription - the stored entry
        action       - "added" or "updated"
    """
    store = _store()
    existing = store.get(url)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    sub = store.subscribe(url, name=name or url, tags=tag_list)
    return {
        "action": "updated" if existing else "added",
        "subscription": {
            "url": sub.url,
            "name": sub.name,
            "tags": sub.tags,
            "added_at": sub.added_at,
        },
        "total_subscriptions": len(store.list()),
    }


@mcp.tool
def unsubscribe_feed(url: str) -> dict:
    """Remove a feed subscription by URL.

    Args:
        url: The RSS/Atom feed URL to remove.

    Returns:
        removed              - True if it was present and removed
        remaining            - number of remaining subscriptions
    """
    store = _store()
    removed = store.unsubscribe(url)
    return {
        "removed": removed,
        "url": url,
        "remaining": len(store.list()),
    }


@mcp.tool
def feed_status() -> dict:
    """Summary of the blog connector state.

    Returns:
        subscriptions        - number of stored subscriptions
        connector_registered - True if "blog" is in the connector registry
        readability_lxml     - True if the optional readability-lxml library is installed
        subs_path            - path to the subscription JSON file
    """
    subs = _store().list()

    try:
        from src.ingestion.connectors.registry import is_registered
        registered = is_registered("blog")
    except Exception:
        registered = False

    try:
        import importlib
        importlib.import_module("readability")
        has_readability = True
    except ImportError:
        has_readability = False

    return {
        "subscriptions": len(subs),
        "connector_registered": registered,
        "readability_lxml": has_readability,
        "subs_path": str(SUBS_PATH),
        "subs_exist": SUBS_PATH.exists(),
    }


@mcp.tool
def ingest_feeds(
    limit_per_feed: int = 20,
    fetch_full_text: bool = False,
    extract_entities: bool = False,
    feed_urls: Optional[str] = None,
) -> dict:
    """Trigger a live ingestion run against all subscribed feeds.

    Fetches the latest posts and optionally runs entity extraction.
    Does NOT write to the warehouse database.

    Args:
        limit_per_feed:   Max posts to ingest per feed (default 20).
        fetch_full_text:  Follow article URLs for full-body extraction
                          (slower — one HTTP request per post).
        extract_entities: Run AdvancedEntityExtractor + KG ingestion
                          (requires spaCy; silently skipped if unavailable).
        feed_urls:        JSON array of URL strings to ingest instead of all
                          subscriptions, e.g. ``'["https://example.com/rss"]'``.

    Returns:
        feeds_fetched        - number of feeds processed
        documents_ingested   - total posts parsed
        entities_extracted   - KG entities stored (0 if extract_entities=False)
        relationships_extracted
        errors               - list of per-feed error messages
    """
    from src.ingestion.connectors.blog.pipeline import ingest_feeds as _ingest

    query = None
    if feed_urls:
        import json as _json
        try:
            query = _json.loads(feed_urls)
        except Exception:
            return {"error": f"feed_urls must be a JSON array, got: {feed_urls!r}"}

    result = _ingest(
        query=query,
        limit_per_feed=limit_per_feed,
        fetch_full_text=fetch_full_text,
        extract_entities=extract_entities,
        subs_path=SUBS_PATH,
    )
    return {
        "feeds_fetched": result.feeds_fetched,
        "documents_ingested": result.documents_ingested,
        "entities_extracted": result.entities_extracted,
        "relationships_extracted": result.relationships_extracted,
        "errors": result.errors,
    }


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"matches": {"type": "array"}, "feeds_checked": {"type": "integer"}},
        "additionalProperties": True,
    },
)
def run_watchlist(
    keywords: str,
    tag_filter: str = "",
    limit_per_feed: int = 20,
    fetch_full_text: bool = False,
) -> dict:
    """Fetch the latest posts and immediately run keyword matching.

    Args:
        keywords:       Comma-separated keywords, e.g. ``"machine learning,python,AI"``.
        tag_filter:     Comma-separated feed tags to scope the search, e.g.
                        ``"tech,research"``.  Empty = search all feeds.
        limit_per_feed: Max posts to pull per feed before matching.
        fetch_full_text: Follow article URLs for full body (slower but more thorough).

    Returns:
        matches     - list of {document_id, title, url, matched_keywords}
        total       - number of matched posts
        keywords    - normalised keyword list used
        tag_filter  - tag filter applied
        feeds_checked - number of subscriptions scanned
    """
    from src.ingestion.connectors.blog.connector import BlogConnector
    from src.ingestion.connectors.blog.digest import match_watchlist as _match

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        return {"error": "keywords must be a non-empty comma-separated list"}

    tag_list = [t.strip() for t in tag_filter.split(",") if t.strip()] or None

    connector = BlogConnector(
        subs_path=SUBS_PATH,
        fetch_full_text=fetch_full_text,
        limit_per_feed=limit_per_feed,
    )

    subs = connector.list_subscriptions()
    docs = list(connector.harvest())
    matches = _match(docs, kw_list, "mcp-watchlist", tag_filter=tag_list)

    return {
        "matches": [
            {
                "document_id": m.document_id,
                "title": m.title,
                "url": m.url,
                "matched_keywords": m.matched_keywords,
            }
            for m in matches
        ],
        "total": len(matches),
        "keywords": kw_list,
        "tag_filter": tag_list or [],
        "feeds_checked": len(subs),
        "posts_scanned": len(docs),
    }


@mcp.tool
def harvest_feed(url: str, name: str = "", limit: int = 10) -> dict:
    """Fetch and parse a single feed URL without persisting a subscription.

    Good for previewing a feed before subscribing.

    Args:
        url:   Full RSS/Atom feed URL.
        name:  Optional display name (defaults to the URL).
        limit: Max posts to return (default 10).

    Returns:
        feed_url  - the URL that was fetched
        count     - number of posts parsed
        posts     - list of post summaries (id, title, url, content_preview, authors, date)
        error     - present only if the fetch or parse failed
    """
    from src.ingestion.connectors.blog.connector import BlogConnector
    from src.ingestion.connectors.blog.models import FeedSubscription

    connector = BlogConnector(
        subs_path=SUBS_PATH,
        fetch_full_text=False,
        limit_per_feed=limit,
    )

    sub = FeedSubscription(url=url, name=name or url, tags=[])
    try:
        docs = list(connector.harvest([sub]))
    except Exception as exc:
        return {"feed_url": url, "count": 0, "posts": [], "error": str(exc)}

    return {
        "feed_url": url,
        "count": len(docs),
        "posts": [_doc_summary(d) for d in docs],
    }


if __name__ == "__main__":
    mcp.run()
