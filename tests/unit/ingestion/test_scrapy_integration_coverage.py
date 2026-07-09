"""
Coverage-focused tests for src/ingestion/scrapy_integration.py.

Targets the sentiment scoring paths (VADER + fallback lexicon), HTTP fetch,
date parsing fallbacks, feed fetching with failures, and the ingest/store/main
entry points. External I/O (urlopen) and the DuckDB warehouse are mocked.
"""

import sys
import types
from datetime import datetime

import pytest

import src.ingestion.scrapy_integration as si


@pytest.fixture(autouse=True)
def _reset_vader_cache():
    """Reset the module-level VADER cache between tests."""
    si._VADER = None
    si._VADER_TRIED = False
    yield
    si._VADER = None
    si._VADER_TRIED = False


# --------------------------------------------------------------------------- #
# Sentiment: VADER present
# --------------------------------------------------------------------------- #

def test_score_sentiment_uses_vader_when_available(monkeypatch):
    class _FakeAnalyzer:
        def polarity_scores(self, text):
            return {"compound": 0.8}

    monkeypatch.setattr(si, "_get_vader", lambda: _FakeAnalyzer())
    score, label = si.score_sentiment("great wonderful news")
    assert score == 0.8
    assert label == "positive"


def test_score_sentiment_empty_text_is_neutral():
    assert si.score_sentiment("") == (0.0, "neutral")
    assert si.score_sentiment("   ") == (0.0, "neutral")


# --------------------------------------------------------------------------- #
# Sentiment: fallback lexicon (VADER unavailable)
# --------------------------------------------------------------------------- #

def test_score_sentiment_fallback_positive(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    # "gain", "growth", "surge" are positive words.
    score, label = si.score_sentiment("Markets gain growth and surge today")
    assert score > 0.05
    assert label == "positive"


def test_score_sentiment_fallback_negative(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    # "loss", "crash", "crisis", "recession" are negative words.
    score, label = si.score_sentiment("Market loss crash crisis recession fear")
    assert score < -0.05
    assert label == "negative"


def test_score_sentiment_fallback_neutral(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    score, label = si.score_sentiment("the report describes ordinary matters")
    assert label == "neutral"
    assert -0.05 <= score <= 0.05


# --------------------------------------------------------------------------- #
# _get_vader
# --------------------------------------------------------------------------- #

def test_get_vader_caches_after_failure(monkeypatch):
    # nltk import fails -> returns None and marks _VADER_TRIED.
    monkeypatch.setitem(sys.modules, "nltk", None)
    assert si._get_vader() is None
    assert si._VADER_TRIED is True
    # Second call short-circuits via the cache.
    assert si._get_vader() is None


def test_get_vader_success(monkeypatch):
    calls = {"n": 0}

    class _Analyzer:
        def __init__(self):
            calls["n"] += 1

    nltk_mod = types.ModuleType("nltk")
    vader_mod = types.ModuleType("nltk.sentiment.vader")
    vader_mod.SentimentIntensityAnalyzer = _Analyzer
    sentiment_mod = types.ModuleType("nltk.sentiment")
    sentiment_mod.vader = vader_mod
    nltk_mod.sentiment = sentiment_mod
    monkeypatch.setitem(sys.modules, "nltk", nltk_mod)
    monkeypatch.setitem(sys.modules, "nltk.sentiment", sentiment_mod)
    monkeypatch.setitem(sys.modules, "nltk.sentiment.vader", vader_mod)

    analyzer = si._get_vader()
    assert analyzer is not None
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# HTTP fetch
# --------------------------------------------------------------------------- #

def test_http_get_reads_response(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<rss>data</rss>"

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(si, "urlopen", _fake_urlopen)
    out = si._http_get("http://example.com/feed")
    assert out == b"<rss>data</rss>"
    assert captured["timeout"] == si.HTTP_TIMEOUT
    # UA now rotates through a realistic pool (#880).
    assert captured["ua"] in si._USER_AGENTS


# --------------------------------------------------------------------------- #
# HTML strip + date parsing
# --------------------------------------------------------------------------- #

def test_strip_html_collapses_whitespace():
    assert si._strip_html("<p>a\n\n  b</p>") == "a b"
    assert si._strip_html(None) == ""


def test_parse_date_rfc822():
    dt = si._parse_date("Mon, 01 Jan 2024 12:00:00 +0000")
    assert dt.year == 2024 and dt.month == 1 and dt.day == 1


def test_parse_date_iso8601():
    dt = si._parse_date("2023-06-15T08:30:00Z")
    assert dt.year == 2023 and dt.month == 6 and dt.day == 15


def test_parse_date_unparseable_falls_back_to_now():
    before = datetime.now()
    dt = si._parse_date("total nonsense not a date")
    assert isinstance(dt, datetime)
    # now() fallback -> not before we started the test.
    assert dt >= before.replace(microsecond=0)


def test_parse_date_none_returns_now():
    assert isinstance(si._parse_date(None), datetime)


# --------------------------------------------------------------------------- #
# Feed parsing
# --------------------------------------------------------------------------- #

RSS = (
    '<?xml version="1.0"?><rss><channel>'
    '<item><title>Stocks surge on strong growth</title>'
    '<link>http://ex.com/a</link>'
    '<description>Markets <b>gain</b> broadly.</description>'
    '<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>'
    '<item><title>No link item</title></item>'
    '</channel></rss>'
)

ATOM = (
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    '<entry><title>Atom Entry</title>'
    '<link href="http://ex.com/atom"/>'
    '<summary>a decline and crisis loom</summary>'
    '<published>2023-01-01T00:00:00Z</published></entry>'
    '</feed>'
)


def test_parse_feed_rss(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    feed = si.Feed("Ex", "http://ex.com/rss", "World")
    articles = si.parse_feed(RSS.encode(), feed)
    # Second item has no link -> dropped.
    assert len(articles) == 1
    art = articles[0]
    assert art.title == "Stocks surge on strong growth"
    assert art.url == "http://ex.com/a"
    assert art.source == "Ex"
    assert art.category == "World"
    assert art.id.startswith("rss-")
    assert art.sentiment_label == "positive"


def test_parse_feed_atom(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    feed = si.Feed("AtomFeed", "http://ex.com/atom.xml", "Economy")
    articles = si.parse_feed(ATOM.encode(), feed)
    assert len(articles) == 1
    assert articles[0].title == "Atom Entry"
    assert articles[0].url == "http://ex.com/atom"


def test_parse_feed_bad_xml_returns_empty():
    feed = si.Feed("Bad", "http://x", "World")
    assert si.parse_feed(b"<<not xml", feed) == []


def test_build_article_requires_title_and_link():
    feed = si.Feed("F", "u", "World")
    assert si._build_article("", "http://x", "d", None, feed) is None
    assert si._build_article("Title", "", "d", None, feed) is None


# --------------------------------------------------------------------------- #
# fetch_articles
# --------------------------------------------------------------------------- #

def test_fetch_articles_skips_failing_feeds(monkeypatch):
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    good = si.Feed("Good", "http://good", "World")
    bad = si.Feed("Bad", "http://bad", "World")

    def _fake_http_get(url):
        if url == "http://bad":
            raise RuntimeError("network down")
        return RSS.encode()

    monkeypatch.setattr(si, "_http_get", _fake_http_get)
    articles = si.fetch_articles([good, bad], limit_per_feed=5)
    # Only the good feed contributed; bad one was skipped.
    assert len(articles) == 1
    assert articles[0].source == "Good"


def test_fetch_articles_default_feeds(monkeypatch):
    monkeypatch.setattr(si, "_http_get", lambda url: RSS.encode())
    monkeypatch.setattr(si, "_get_vader", lambda: None)
    # Passing feeds=None uses DEFAULT_FEEDS; each yields the same RSS.
    articles = si.fetch_articles(None, limit_per_feed=1)
    assert len(articles) == len(si.DEFAULT_FEEDS)


# --------------------------------------------------------------------------- #
# store_articles / ingest / main (warehouse mocked)
# --------------------------------------------------------------------------- #

class _FakeConn:
    def __init__(self):
        self.deleted = []
        self.inserted = []
        self._existing_ids = []
        self._count = 0

    def execute(self, sql, *args):
        self._last_sql = sql
        if sql.strip().startswith("DELETE"):
            self.deleted.append(sql.strip())
        return self

    def executemany(self, sql, rows):
        self.inserted.extend(rows)
        return self

    def fetchall(self):
        return [(i,) for i in self._existing_ids]

    def fetchone(self):
        return (self._count,)


def _install_fake_warehouse(monkeypatch, conn):
    db_conn = types.ModuleType("src.database.local_analytics_connector")
    db_conn.get_shared_connection = lambda: conn
    db_seed = types.ModuleType("src.database.local_warehouse_seed")
    db_seed.ensure_schema = lambda c: None
    monkeypatch.setitem(sys.modules, "src.database.local_analytics_connector", db_conn)
    monkeypatch.setitem(sys.modules, "src.database.local_warehouse_seed", db_seed)


def _make_article(id_="rss-1", url="http://a"):
    return si.Article(
        id=id_, title="t", url=url, content="c",
        publish_date=datetime(2024, 1, 1), source="s",
        category="World", sentiment_score=0.0, sentiment_label="neutral",
    )


def test_store_articles_inserts_only_new(monkeypatch):
    conn = _FakeConn()
    conn._existing_ids = ["rss-existing"]
    _install_fake_warehouse(monkeypatch, conn)
    arts = [_make_article("rss-existing"), _make_article("rss-new", "http://b")]
    n = si.store_articles(arts, replace=False)
    assert n == 1  # only rss-new inserted
    assert len(conn.inserted) == 1
    # Non-replace path deletes the synthetic seed.
    assert any("art-%" in d for d in conn.deleted)


def test_store_articles_replace_clears_table(monkeypatch):
    conn = _FakeConn()
    _install_fake_warehouse(monkeypatch, conn)
    n = si.store_articles([_make_article()], replace=True)
    assert n == 1
    assert any(d == "DELETE FROM news_articles" for d in conn.deleted)


def test_ingest_returns_summary(monkeypatch):
    conn = _FakeConn()
    conn._count = 7
    _install_fake_warehouse(monkeypatch, conn)
    monkeypatch.setattr(si, "fetch_articles", lambda feeds, limit_per_feed, **kw: [_make_article()])
    stats = si.ingest(limit_per_feed=3, replace=False)
    assert stats["fetched"] == 1
    assert stats["inserted"] == 1
    assert stats["total_in_warehouse"] == 7


def test_main_success(monkeypatch):
    monkeypatch.setattr(
        si, "ingest",
        lambda limit_per_feed, replace, **kw: {
            "fetched": 2, "inserted": 2, "total_in_warehouse": 2,
        },
    )
    assert si.main(["--limit", "10"]) == 0


def test_main_replace_flag_passed(monkeypatch):
    captured = {}

    def _fake_ingest(limit_per_feed, replace, **kw):
        captured["limit"] = limit_per_feed
        captured["replace"] = replace
        return {"fetched": 0, "inserted": 0, "total_in_warehouse": 0}

    monkeypatch.setattr(si, "ingest", _fake_ingest)
    assert si.main(["--limit", "40", "--replace"]) == 0
    assert captured["limit"] == 40
    assert captured["replace"] is True


def test_main_handles_failure(monkeypatch):
    def _boom(limit_per_feed, replace, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(si, "ingest", _boom)
    assert si.main([]) == 1
