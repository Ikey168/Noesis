---
name: scaffold-blog-watchlist
description: Scaffold a new blog watchlist for Noesis. Use when you want to monitor a set of keywords across subscribed RSS/Atom feeds and surface matching posts as a digest. Covers FeedSubscription setup, WatchlistEntry definition, running the watchlist via the blog-feeds MCP, and optionally wiring a digest endpoint into the API.
---

# Scaffold a Noesis blog watchlist

A watchlist monitors a set of keywords across all ingested blog/RSS/Atom posts.
When any post's title or content contains a keyword, it surfaces in the
**digest** — a list of `DigestMatch` objects showing the post, URL, and which
keywords matched.

This skill pairs with the **`neuronews-blog-feeds`** MCP server (in `.mcp.json`):
- `subscribe_feed(url, name, tags)` — add a feed subscription
- `ingest_feeds(limit_per_feed=20)` — pull latest posts from all subscriptions
- `run_watchlist(keywords, tag_filter?)` — run keyword matching on latest posts

**Paths below are relative to the repo root** (`/home/Ikey/NeuroNews`).

## Quick path: run a watchlist from the MCP

The fastest way to monitor keywords against already-subscribed feeds:

```
# 1. Subscribe to a feed (if not already subscribed)
subscribe_feed("https://example.com/rss", name="Example Blog", tags=["tech"])

# 2. Pull latest posts
ingest_feeds(limit_per_feed=10, fetch_full_text=False)

# 3. Run the watchlist
run_watchlist(["machine learning", "python", "AI"], tag_filter=["tech"])
# → {"matches": [...], "total": 3, "feeds_checked": 1}
```

## Full path: a named, persistent watchlist

For watchlists you want to run repeatedly or expose via the API, define them
in code using the `WatchlistEntry` dataclass:

```python
# src/domains/<your_domain>/watchlists.py
from src.ingestion.connectors.blog.models import WatchlistEntry

AI_WATCHLIST = WatchlistEntry(
    name="ai-research",
    keywords=["machine learning", "large language model", "neural network", "GPT"],
    tags=["tech", "research"],   # scope to feeds tagged with these
)

CLIMATE_WATCHLIST = WatchlistEntry(
    name="climate",
    keywords=["climate change", "global warming", "carbon emissions", "net zero"],
    tags=[],  # empty = search all feeds
)
```

Run them against a batch of `Document` objects:

```python
from src.ingestion.connectors.blog.digest import run_watchlists
from src.ingestion.connectors.blog import ingest_feeds

# Ingest latest posts
result = ingest_feeds(fetch_full_text=True, limit_per_feed=20)

# Then run against the documents you just harvested
# (or pull from your document store)
from src.ingestion.connectors.blog import BlogConnector
docs = list(BlogConnector().harvest())

digest = run_watchlists(docs, [AI_WATCHLIST, CLIMATE_WATCHLIST])
# → {"ai-research": [DigestMatch(...)], "climate": [...]}
```

## DigestMatch fields

```python
@dataclass
class DigestMatch:
    document_id: str         # stable blog-<hash> id
    title: str               # post title
    url: str                 # post URL
    matched_keywords: list   # which keywords matched (lowercase)
    watchlist_name: str      # which watchlist produced this match
```

## Subscribing to feeds

Use `SubscriptionStore` or the MCP to manage subscriptions. Subscriptions
persist in `config/blog_subscriptions.json`:

```python
from src.ingestion.connectors.blog import BlogConnector

connector = BlogConnector()
connector.subscribe("https://huggingface.co/blog/feed.xml", name="HuggingFace Blog", tags=["tech", "research"])
connector.subscribe("https://openai.com/blog/rss", name="OpenAI Blog", tags=["tech"])
connector.subscribe("https://www.carbonbrief.org/feed/", name="Carbon Brief", tags=["climate"])
```

Or from the MCP:
```
subscribe_feed("https://huggingface.co/blog/feed.xml", name="HuggingFace Blog", tags=["tech", "research"])
```

## Tag scoping

Tags let you scope a watchlist to a subset of feeds. A post must come from a
feed whose metadata `tags` overlap with the watchlist's `tags` to be eligible.
Empty `tags` = match all feeds.

```python
# Only surfaces in feeds tagged ["tech"] or ["research"]
WatchlistEntry(name="llm-news", keywords=["LLM", "transformer"], tags=["tech", "research"])

# Surfaces across every subscribed feed
WatchlistEntry(name="global-ai", keywords=["artificial intelligence"], tags=[])
```

## Wiring a digest endpoint into the API

Add a route to `src/api/routes/document_routes.py` (the always-mounted generic
router) or, if news-specific, gate it behind `is_pack_enabled("news")` in
`src/api/app.py`.

Minimal digest endpoint:

```python
# src/api/routes/document_routes.py (add after existing routes)
from src.ingestion.connectors.blog.digest import match_watchlist

@router.get("/documents/digest")
def digest_endpoint(keywords: str, tags: str = ""):
    """Run an ad-hoc keyword watchlist against the in-memory document store."""
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    tag_filter = [t.strip() for t in tags.split(",") if t.strip()] or None
    docs = list(_store.values())  # replace with your real store
    matches = match_watchlist(docs, kws, "ad-hoc", tag_filter)
    return {
        "query": {"keywords": kws, "tag_filter": tag_filter},
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
    }
```

## Verify checklist

1. `subscribe_feed(...)` via MCP → appears in `list_subscriptions()`
2. `ingest_feeds(limit_per_feed=5)` → `documents_ingested > 0`, `errors == []`
3. `run_watchlist(["your keyword"])` → returns matching posts (or empty if no matches yet)
4. `(optional)` `GET /documents/digest?keywords=your+keyword` → same matches via API

## Gotchas

- **Keyword matching is case-insensitive substring search** — `"AI"` matches
  "AI", "ai", "AIsee", "main". Use longer phrases for precision.
- **`fetch_full_text=True` is slower** — each post URL is fetched individually.
  For watchlists running on a schedule, set `fetch_full_text=False` for fast
  first-pass matching on feed summaries; re-fetch full text only for matches.
- **`config/blog_subscriptions.json` is the truth** — subscriptions are not in
  the database. Back it up alongside `config/domain_packs.json`.
- **Feed summaries are truncated** — many feeds only publish a one-sentence
  blurb. If your keyword appears in body paragraphs, you need `fetch_full_text=True`
  to catch it.
- **`run_watchlists()` scans in-memory documents** — it does not query a
  database. Pair with `ingest_feeds()` to first pull the latest posts.
