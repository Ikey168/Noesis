from src.ingestion.website_discovery import WebsiteFrontier, discover_candidates
import pytest


def test_sitemap_index_duplicates_and_scope():
    text = "<sitemapindex><sitemap><loc>/sitemap-two.xml</loc></sitemap><sitemap><loc>/sitemap-two.xml</loc></sitemap><sitemap><loc>https://other.org/map</loc></sitemap></sitemapindex>"
    result = discover_candidates(text, "https://example.org/sitemap.xml")
    assert (
        len(result) == 1
        and result[0]["url"] == "https://example.org/sitemap-two.xml"
        and result[0]["kind"] == "sitemap"
    )
    with pytest.raises(Exception):
        discover_candidates("<urlset>", "https://example.org/map")


def test_bounded_restart_retains_completed_receipts(tmp_path):
    calls = []

    def transport(**kw):
        calls.append(kw["url"])
        return {
            "content": '<html><a href="/article">Article</a><a href="https://other.org/no">No</a></html>'
        }

    args = {
        "domain": "research",
        "source": "site",
        "seed": "https://example.org",
        "max_pages": 5,
        "transport": transport,
    }
    frontier = WebsiteFrontier(tmp_path / "frontier.sqlite", **args)
    first = frontier.run(max_steps=1)
    frontier.close()
    assert len(calls) == 1
    frontier = WebsiteFrontier(tmp_path / "frontier.sqlite", **args)
    final = frontier.run()
    frontier.close()
    assert len(calls) == len(set(calls)) and len(calls) <= 5
    assert all(row["state"] == "visited" for row in final)
    assert all("other.org" not in row["url"] for row in final)


def test_local_site_feed_and_sitemap_discovery_survives_restart(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    counts = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            pages = {
                "/": '<html><link rel="alternate" type="application/rss+xml" href="/feed.xml"><a href="/article">Article</a></html>',
                "/robots.txt": "Sitemap: /sitemap.xml",
                "/sitemap.xml": "<urlset><url><loc>/article</loc></url></urlset>",
                "/feed.xml": '<rss version="2.0"><channel><title>Feed</title><item><title>Article</title><link>'
                + base
                + "/article</link></item></channel></rss>",
                "/article": "<html><article>Evidence</article></html>",
            }
            content = pages.get(self.path, "")
            self.send_response(200 if content else 404)
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        args = dict(domain="research", source="fixture", seed=base + "/", max_pages=10)
        first = WebsiteFrontier(tmp_path / "frontier.sqlite", **args)
        first.run(max_steps=1)
        first.close()
        second = WebsiteFrontier(tmp_path / "frontier.sqlite", **args)
        rows = second.run()
        second.close()
        assert len(rows) == 5
        assert all(row["state"] == "visited" for row in rows)
        assert set(counts.values()) == {1}
    finally:
        server.shutdown()
        server.server_close()
