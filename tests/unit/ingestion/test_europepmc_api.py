import json

import pytest

from src.ingestion.europepmc_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import validate_source_pack
from pathlib import Path


def test_native_params_bounds_nested_records_and_cursor():
    params=parameters({'query':'cancer','pageSize':9999,'resultType':'lite'},cursor='cursor-2',limit=2)
    assert params=={'query':'cancer','pageSize':2,'resultType':'core','format':'json','cursorMark':'cursor-2'}
    raw={'id':'123','source':'MED','title':'A &amp; B','abstractText':'<h4>Results</h4><p>A rose &amp; B fell.</p>',
        'pubTypeList':{'pubType':['Retracted Publication']},'authorList':{'author':[{'fullName':'Example Author'}]}}
    mapped,cursor=records({'resultList':{'result':[raw]},'nextCursorMark':'last','hitCount':1},cursor=None,limit=1)
    assert cursor is None and mapped[0]['id']=='MED:123'
    assert mapped[0]['content']=='Results\n\nA rose & B fell.' and mapped[0]['retracted']
    assert mapped[0]['abstractText']==raw['abstractText']
    notice={**raw,'pubTypeList':{'pubType':['Retraction of Publication']}}
    assert not records({'resultList':{'result':[notice]}},cursor=None,limit=1)[0][0]['retracted']
    with pytest.raises(ValueError,match='lacks'):
        records({'unexpected':'data'},cursor=None,limit=1)
    with pytest.raises(ValueError,match='budget'):
        records({'resultList':{'result':[raw,raw]}},cursor=None,limit=1)


def test_runtime_adapter_maps_abstract_instead_of_response_envelope():
    manifest=validate_source_pack(json.loads(Path('config/source_packs/scientific.json').read_text()))
    source=next(v for v in manifest['sources'] if v['source_id']=='europe-pmc')
    requests=[]
    def transport(**request):
        requests.append(request)
        return {'status':200,'content':json.dumps({'hitCount':1,'resultList':{'result':[{'id':'123','source':'MED','title':'A paper','abstractText':'Its exact abstract.'}]}})}
    page=HTTPSPageAdapter(source,transport=transport).fetch_page({'operation':'identifier','parameters':{'query':'EXT_ID:123'},'limit':1},cursor=None)
    assert requests[0]['params']['pageSize']==1 and requests[0]['params']['resultType']=='core'
    assert len(page.records)==1 and page.records[0]['content']=='Its exact abstract.'
    assert page.next_cursor is None


def test_live_transport_preserves_retryable_status_and_sanitizes_failures(monkeypatch):
    import urllib.error
    from types import SimpleNamespace
    import pytest
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter
    from src.ingestion.source_packs import SourcePackError
    def fail(exc):
        def open(*args,**kwargs):raise exc
        monkeypatch.setattr('urllib.request.build_opener',lambda *args:SimpleNamespace(open=open))
    fail(TimeoutError('private transport detail'))
    with pytest.raises(SourcePackError) as error:
        HTTPSPageAdapter._request(url='https://example.org',params={},headers={},timeout=.1)
    assert error.value.code=='source_timeout' and 'private' not in str(error.value)
    fail(urllib.error.HTTPError('https://example.org',429,'rate limit',{'Retry-After':'10'},None))
    response=HTTPSPageAdapter._request(url='https://example.org',params={},headers={},timeout=.1)
    assert response['status']==429 and response['headers']['Retry-After']=='10' and response['content']==b''
