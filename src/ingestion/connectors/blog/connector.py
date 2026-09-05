"""Blog / RSS / Atom connector.

Subscribes to arbitrary RSS/Atom feeds and produces ``Document`` records with
``source_type="blog"``.  Optionally fetches full article body via readability
extraction so the document carries more than the truncated feed summary.

Flow:
    discover()  — yields one SourceRef per subscription (or per query)
    fetch()     — fetches RSS/Atom bytes for a feed URL
    parse()     — feedparser → N Documents; optionally enriches content from
                  the article URL via :func:`fetch_full_text`
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.blog.models import FeedSubscription
from src.ingestion.connectors.blog.subscriptions import SubscriptionStore
from src.ingestion.connectors.registry import register_connector

logger = logging.getLogger(__name__)

_USER_AGENT = "NeuroNewsBot/1.0 (+https://github.com/Ikey168/NeuroNews)"
_HTTP_TIMEOUT = 15


def _default_http_get(url: str) -> bytes:
    from urllib.request import Request, urlopen

    req = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_millis(struct_time) -> Optional[int]:
    """Convert a ``time.struct_time`` (from feedparser) to epoch-ms."""
    if struct_time is None:
        return None
    try:
        dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _stable_id(url: str, title: str = "") -> str:
    key = url or title
    return "blog-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _normalize_query(query: Any) -> List[FeedSubscription]:
    """Accept URL strings, FeedSubscription objects, or dicts."""
    result: List[FeedSubscription] = []
    for item in query:
        if isinstance(item, FeedSubscription):
            result.append(item)
        elif isinstance(item, dict):
            result.append(FeedSubscription(
                url=item["url"],
                name=item.get("name", item["url"]),
                tags=list(item.get("tags", [])),
            ))
        else:
            result.append(FeedSubscription(url=str(item), name=str(item)))
    return result


@register_connector
class BlogConnector(Connector):
    """Ingest blog / newsletter / podcast RSS-Atom feeds as document-ingest-v1 records.

    Args:
        subs_path: Path to the JSON subscription store (default: ``config/blog_subscriptions.json``).
        fetch_full_text: When True, follow each entry URL to extract the full
            article body.  Falls back to feed summary silently on any error.
        limit_per_feed: Maximum entries to ingest per feed per run.
        http_get: Callable ``(url: str) -> bytes`` injected in tests.
        full_text_getter: Callable ``(url: str) -> str`` injected in tests.
    """

    source_type = "blog"

    def __init__(
        self,
        subs_path=None,
        fetch_full_text: bool = True,
        limit_per_feed: int = 20,
        http_get: Optional[Callable[[str], bytes]] = None,
        full_text_getter: Optional[Callable[[str], str]] = None,
        http_state_path=None,
        response_transport=None,
    ) -> None:
        self._store = SubscriptionStore(subs_path)
        self._fetch_full_text = fetch_full_text
        self._limit = limit_per_feed
        self._http_get = http_get or _default_http_get
        self._http_state = None
        if http_get is None:
            from src.ingestion.connectors.blog.http_cache import FeedHTTPStore
            self._http_state = FeedHTTPStore(http_state_path or self._store._path.with_suffix('.http.sqlite'), transport=response_transport)
        if full_text_getter is not None:
            self._full_text_getter: Callable[[str], str] = full_text_getter
        else:
            from src.ingestion.connectors.blog.readability import fetch_full_text as _ft
            self._full_text_getter = _ft

    # ------------------------------------------------------------------ #
    # Connector interface
    # ------------------------------------------------------------------ #

    def discover(self, query: Optional[Any] = None) -> Iterable[SourceRef]:
        """Yield one SourceRef per subscription.

        ``query`` may be a list of URL strings, :class:`FeedSubscription`
        objects, or ``{"url": ..., "name": ..., "tags": [...]}`` dicts.
        When omitted, subscriptions are read from the store.
        """
        subs = _normalize_query(query) if query is not None else self._store.list()
        for sub in subs:
            yield SourceRef(
                locator=sub.url,
                title=sub.name or sub.url,
                metadata={"name": sub.name or sub.url, "tags": list(sub.tags)},
            )

    def fetch(self, ref: SourceRef) -> RawDocument:
        if self._http_state is not None:
            content, metadata = self._http_state.fetch(ref.locator)
        else:
            content, metadata = self._http_get(ref.locator), {}
        return RawDocument(ref=ref, content=content, content_type="application/xml", metadata=metadata)

    def parse(self, raw: RawDocument) -> List[Document]:
        if raw.metadata.get('outcome') == 'unchanged':
            return []
        import feedparser

        data = raw.content
        if isinstance(data, str):
            data = data.encode("utf-8")

        parsed = feedparser.parse(data)
        feed_name = (
            raw.ref.metadata.get("name")
            or parsed.feed.get("title", "")
            or raw.ref.title
            or raw.ref.locator
        )
        tags = raw.ref.metadata.get("tags", [])
        ingested_at = raw.fetched_at

        documents: List[Document] = []
        for entry in parsed.entries[: self._limit]:
            doc = self._entry_to_document(entry, feed_name, tags, ingested_at)
            if doc is not None:
                documents.append(doc)
        return documents

    # ------------------------------------------------------------------ #
    # Subscription helpers (pass-through to the store)
    # ------------------------------------------------------------------ #

    def subscribe(self, url: str, name: str = "", tags: Optional[List[str]] = None) -> FeedSubscription:
        """Persist a new subscription."""
        return self._store.subscribe(url, name=name, tags=tags)

    def unsubscribe(self, url: str) -> bool:
        return self._store.unsubscribe(url)

    def list_subscriptions(self) -> List[FeedSubscription]:
        return self._store.list()

    # ------------------------------------------------------------------ #
    # Entity extraction
    # ------------------------------------------------------------------ #

    def ingest_to_kg(self, document: Document, store=None) -> Dict[str, Any]:
        """Extract entities from ``document`` and store them in the KG.

        Returns ``{"entities": int, "relationships": int}``.
        Gracefully returns zeros if spaCy or the KG store is unavailable.
        """
        try:
            import asyncio
            from src.knowledge_graph.enhanced_entity_extractor import AdvancedEntityExtractor
            from src.knowledge_graph.foundation import KnowledgeGraphStore, Node
            from src.knowledge_graph.foundation.ontology import EntityType

            kg_store = store if store is not None else KnowledgeGraphStore()
            extractor = AdvancedEntityExtractor()

            # spaCy-label → EntityType; unknown labels map to CONCEPT.
            _label_map = {
                "PERSON": EntityType.PERSON,
                "ORG": EntityType.ORGANIZATION,
                "GPE": EntityType.CONCEPT,
                "LOC": EntityType.CONCEPT,
                "FAC": EntityType.CONCEPT,
                "NORP": EntityType.ORGANIZATION,
            }

            async def _run() -> Dict[str, int]:
                entities = await extractor.extract_entities_from_article(
                    document.document_id,
                    document.title or "",
                    document.content or "",
                )
                rels = await extractor.extract_relationships(
                    entities,
                    (document.title or "") + " " + (document.content or ""),
                    document.document_id,
                )
                for ent in entities:
                    etype = _label_map.get(ent.label, EntityType.CONCEPT)
                    node = Node(
                        type=etype,
                        name=ent.normalized_form or ent.text,
                        node_id=ent.entity_id or "",
                        aliases=list(ent.aliases),
                        properties={"source_article": document.document_id, "confidence": ent.confidence},
                    )
                    kg_store.add_node(node)
                return {"entities": len(entities), "relationships": len(rels)}

            return asyncio.run(_run())
        except Exception as exc:
            logger.debug("ingest_to_kg skipped: %s", exc)
            return {"entities": 0, "relationships": 0}

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _entry_to_document(
        self,
        entry: Any,
        feed_name: str,
        feed_tags: List[str],
        ingested_at: int,
    ) -> Optional[Document]:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not title and not url:
            return None

        # Summary from feed (may contain HTML).
        content_list = entry.get("content") or []
        content_value = content_list[0].value if content_list and hasattr(content_list[0], "value") else ""
        raw_summary = (
            entry.get("summary")
            or entry.get("description")
            or content_value
        )
        summary = _strip_html(raw_summary)

        # Full article body.
        content = summary
        if self._fetch_full_text and url:
            try:
                full = self._full_text_getter(url)
                if full and len(full) > len(summary):
                    content = full
            except Exception:
                pass

        # Authors.
        author = entry.get("author") or ""
        authors = [a.strip() for a in author.split(",") if a.strip()] if author else []

        # Entry tags/categories from the feed.
        entry_tags = [t.get("term", "") for t in (entry.get("tags") or []) if t.get("term")]

        return Document(
            document_id=_stable_id(url, title),
            source_type="blog",
            language="en",
            ingested_at=ingested_at,
            source_id=feed_name,
            url=url or None,
            title=title or None,
            content=content or None,
            authors=authors,
            created_at=_to_millis(entry.get("published_parsed") or entry.get("updated_parsed")),
            metadata={
                "feed_name": feed_name,
                "tags": list(set(feed_tags + entry_tags)),
                "has_full_text": content != summary and bool(content),
            },
        )
