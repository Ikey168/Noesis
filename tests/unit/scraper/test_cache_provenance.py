from types import SimpleNamespace
from scrapy.http import Request, Response
from scrapy.settings import Settings
from src.scraper.cache_provenance import ProvenanceCacheMiddleware


def test_offline_replay_keeps_original_times_and_live_304_advances_check():
    cached = Response(
        "https://example.org",
        headers={"X-Noesis-Fetched-At": "10", "X-Noesis-Revalidated-At": "20"},
        body=b"old",
    )
    stored = []
    middleware = object.__new__(ProvenanceCacheMiddleware)
    middleware.crawler = SimpleNamespace(
        settings=Settings({"NOESIS_OFFLINE_REPLAY": True}), spider=object()
    )
    middleware.storage = SimpleNamespace(
        retrieve_response=lambda *a: cached,
        store_response=lambda *a: stored.append(a[-1]),
    )
    middleware.stats = SimpleNamespace(inc_value=lambda *a: None)
    middleware.policy = SimpleNamespace(is_cached_response_valid=lambda *a: True)
    request = Request(cached.url)
    result = middleware.process_request(request)
    middleware.process_response(request, result)
    assert request.meta["acquisition_provenance"] == {
        "mode": "offline-replay",
        "original_fetched_at": "10",
        "last_revalidated_at": "20",
    }
    request = Request(cached.url, meta={"cached_response": cached})
    middleware.crawler.settings.set("NOESIS_OFFLINE_REPLAY", False)
    result = middleware.process_response(request, Response(cached.url, status=304))
    assert result.body == b"old" and stored
    assert request.meta["acquisition_provenance"]["original_fetched_at"] == "10"
    assert int(request.meta["acquisition_provenance"]["last_revalidated_at"]) > 20
