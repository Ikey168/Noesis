"""Restartable, host-scoped website exploration with bounded acquisition."""

import json
import sqlite3
import time
from urllib.parse import urljoin, urlsplit

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.scraper.article_links import scoped_url


def discover_candidates(text, url):
    from bs4 import BeautifulSoup
    from defusedxml import ElementTree as ET

    candidates = []
    stripped = text.lstrip()
    if stripped.startswith(("<?xml", "<urlset", "<sitemapindex", "<rss", "<feed")):
        root = ET.fromstring(text)
        if root.tag.rsplit("}", 1)[-1] in ("rss", "feed"):
            from trafilatura.feeds import FeedParameters, extract_links

            params = FeedParameters(
                baseurl=url,
                domain=urlsplit(url).hostname,
                reference=url,
                external=False,
            )
            return [
                {"url": target, "kind": "page", "discovered_from": url}
                for value in extract_links(text, params)
                if (target := scoped_url(url, value))
            ]
        if root.tag.rsplit("}", 1)[-1] not in ("urlset", "sitemapindex"):
            raise ValueError("unsupported discovery XML")
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
                candidates.append(
                    (
                        element.text,
                        "sitemap" if root.tag.endswith("sitemapindex") else "page",
                    )
                )
    elif "<" in text:
        soup = BeautifulSoup(text, "html.parser")
        for anchor in soup.select("a[href]"):
            candidates.append((anchor["href"], "page"))
        for link in soup.select('link[rel="alternate"][href]'):
            if link.get("type") in ("application/rss+xml", "application/atom+xml"):
                candidates.append((link["href"], "feed"))
    else:
        from trafilatura.sitemaps import extract_robots_sitemaps

        candidates.extend((v, "sitemap") for v in extract_robots_sitemaps(text, url))
    result = []
    seen = set()
    for value, kind in candidates:
        target = scoped_url(url, value)
        if target and target not in seen:
            result.append({"url": target, "kind": kind, "discovered_from": url})
            seen.add(target)
    return result


class WebsiteFrontier:
    def __init__(
        self,
        path,
        *,
        domain,
        source,
        seed,
        max_depth=2,
        max_pages=50,
        timeout_ms=30000,
        transport=None,
    ):
        if (
            not 0 <= max_depth <= 5
            or not 1 <= max_pages <= 1000
            or not 1 <= timeout_ms <= 300000
        ):
            raise ValueError("invalid discovery budget")
        if urlsplit(seed).scheme not in ("https", "http"):
            raise ValueError("invalid seed")
        self.domain, self.source, self.seed = domain, source, seed
        self.depth, self.pages, self.timeout = max_depth, max_pages, timeout_ms
        self.transport = transport
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS website_frontier(domain TEXT,source TEXT,url TEXT,depth INTEGER,parent TEXT,kind TEXT,state TEXT,receipt TEXT,PRIMARY KEY(domain,source,url))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS website_frontier_config(domain TEXT,source TEXT,config TEXT,PRIMARY KEY(domain,source))"
        )
        config = json.dumps([seed, max_depth, max_pages, timeout_ms])
        old = self.conn.execute(
            "SELECT config FROM website_frontier_config WHERE domain=? AND source=?",
            (domain, source),
        ).fetchone()
        if old and old[0] != config:
            raise ValueError(
                "frontier configuration changed; use a new source identifier"
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO website_frontier_config VALUES(?,?,?)",
            (domain, source, config),
        )
        self._enqueue(seed, 0, None, "page")
        self._enqueue(urljoin(seed, "/robots.txt"), 0, None, "robots")
        self._enqueue(urljoin(seed, "/sitemap.xml"), 0, None, "sitemap")
        self.conn.commit()

    def _enqueue(self, url, depth, parent, kind):
        target = scoped_url(self.seed, url)
        if target and depth <= self.depth:
            count = self.conn.execute(
                "SELECT count(*) FROM website_frontier WHERE domain=? AND source=?",
                (self.domain, self.source),
            ).fetchone()[0]
            if count < self.pages:
                self.conn.execute(
                    "INSERT OR IGNORE INTO website_frontier VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self.domain,
                        self.source,
                        target,
                        depth,
                        parent,
                        kind,
                        "pending",
                        None,
                    ),
                )

    def run(self, *, max_steps=None):
        deadline = time.monotonic() + self.timeout / 1000
        completed = 0
        while (
            completed
            < (self.pages if max_steps is None else min(self.pages, max_steps))
            and time.monotonic() < deadline
        ):
            row = self.conn.execute(
                "SELECT url,depth,kind FROM website_frontier WHERE domain=? AND source=? AND state='pending' ORDER BY depth,url LIMIT 1",
                (self.domain, self.source),
            ).fetchone()
            if not row:
                break
            url, depth, _kind = row
            try:
                getter = self.transport or HTTPSPageAdapter._request
                response = getter(
                    url=url,
                    params={},
                    headers={"Accept": "text/html,application/xml,text/plain"},
                    timeout=max(0.001, min(15, deadline - time.monotonic())),
                    **({} if self.transport else {"max_bytes": 2_000_000}),
                )
                data = response.get("content", b"")
                if len(data) > 2_000_000 or response.get("status", 200) != 200:
                    raise ValueError("discovery response failed")
                text = (
                    data.decode("utf-8", errors="replace")
                    if isinstance(data, bytes)
                    else data
                )
                candidates = discover_candidates(text, url)
                import hashlib

                receipt = {
                    "url": url,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "discovered": candidates,
                    "observed_at_ms": int(time.time() * 1000),
                }
                with self.conn:
                    for candidate in candidates:
                        self._enqueue(
                            candidate["url"], depth + 1, url, candidate["kind"]
                        )
                    self.conn.execute(
                        "UPDATE website_frontier SET state='visited',receipt=? WHERE domain=? AND source=? AND url=?",
                        (json.dumps(receipt), self.domain, self.source, url),
                    )
            except Exception as exc:  # noqa: BLE001 - preserve bounded acquisition failure outcome
                with self.conn:
                    self.conn.execute(
                        "UPDATE website_frontier SET state='failed',receipt=? WHERE domain=? AND source=? AND url=?",
                        (
                            json.dumps(
                                {
                                    "failure_code": getattr(
                                        exc, "code", "discovery_failed"
                                    )
                                }
                            ),
                            self.domain,
                            self.source,
                            url,
                        ),
                    )
            completed += 1
        return self.inspect()

    def inspect(self):
        return [
            dict(zip(("url", "depth", "parent", "kind", "state", "receipt"), row))
            for row in self.conn.execute(
                "SELECT url,depth,parent,kind,state,receipt FROM website_frontier WHERE domain=? AND source=? ORDER BY depth,url",
                (self.domain, self.source),
            )
        ]

    def close(self):
        self.conn.close()
