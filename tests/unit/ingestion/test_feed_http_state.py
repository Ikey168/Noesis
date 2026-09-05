from src.ingestion.connectors.blog.connector import BlogConnector
from src.ingestion.connectors.blog.http_cache import retry_after_seconds
from src.ingestion.source_health import SourceHealthTracker


def test_restart_conditional_unchanged_preserves_health_and_no_documents(tmp_path):
    calls=[]
    def response(url,headers):
        calls.append(headers)
        if len(calls)>1:return 304,{},b''
        return 200,{'ETag':'"version1"','Last-Modified':'Wed, 01 Jan 2025 00:00:00 GMT'},b'<rss version="2.0"><channel><title>Feed</title><item><title>Story</title><link>https://example.org/story</link><description>Text</description></item></channel></rss>'
    kwargs={'subs_path':tmp_path/'subs.json','http_state_path':tmp_path/'http.sqlite','response_transport':response,'fetch_full_text':False}
    health=SourceHealthTracker()
    for i in range(8):health.record_run('https://example.org/feed',5,now_ms=i)
    first=BlogConnector(**kwargs).harvest_run(['https://example.org/feed'],health=health,respect_schedule=False)
    baseline=health.status('https://example.org/feed')
    for _ in range(8):
        second=BlogConnector(**kwargs).harvest_run(['https://example.org/feed'],health=health,respect_schedule=False)
        assert second.unchanged==1 and second.documents==0 and second.parse_errors==0
    assert first.documents==1 and health.status('https://example.org/feed')==baseline
    assert calls[1]['If-None-Match']=='"version1"' and 'If-Modified-Since' in calls[1]


def test_retry_after_delta_and_date():
    assert retry_after_seconds('30',now=0)==30
    assert retry_after_seconds('Thu, 01 Jan 1970 00:00:40 GMT',now=0)==40
    assert retry_after_seconds('nonsense',now=0) is None
