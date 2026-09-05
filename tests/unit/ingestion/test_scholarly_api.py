import json
from pathlib import Path
import pytest
from src.ingestion.scholarly_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import validate_source_pack, SourcePackError


@pytest.mark.parametrize('name', ['crossref', 'openalex'])
def test_native_cursor_pages_and_restart(name):
    source = next(s for s in validate_source_pack(json.loads(Path('config/source_packs/research.json').read_text()))['sources'] if s['source_id'] == name + '-works')
    item = {'DOI':'10.1234/ABC', 'title':['Title'], 'author':[{'given':'A','family':'B'}], 'published':{'date-parts':[[2024,2]]}} if name == 'crossref' else {'id':'https://openalex.org/W1','doi':'https://doi.org/10.1234/ABC','title':'Title','abstract_inverted_index':{'An':[0],'abstract':[1]},'authorships':[{'author':{'display_name':'A B'}}]}
    calls=[]
    def transport(**kw):
        calls.append(kw)
        cursor=kw['params']['cursor']
        values=[item] if cursor in ('*','second') else []
        payload={'message':{'items':values,'next-cursor':'second' if cursor=='*' else 'end'}} if name=='crossref' else {'results':values,'meta':{'next_cursor':'second' if cursor=='*' else 'end'}}
        return {'content':json.dumps(payload)}
    adapter=HTTPSPageAdapter(source,transport=transport,secret='private-key')
    request={'operation':'search','parameters':{'query':'topic'},'limit':1}
    first=adapter.fetch_page(request,cursor=None)
    second=HTTPSPageAdapter(source,transport=transport,secret='private-key').fetch_page(request,cursor=first.next_cursor)
    assert first.records[0]['id']==second.records[0]['id']
    assert first.records[0]['doi']=='10.1234/abc'
    assert first.records[0]['authors']==['A B']
    assert adapter.fetch_page(request,cursor=second.next_cursor).records==()
    assert 'Authorization' not in calls[0]['headers']
    assert 'private-key' not in json.dumps(first.receipt)
    assert 'limit' not in calls[0]['params']
    assert first.records[0]['content_representation']==('title-only' if name=='crossref' else 'plain-text-abstract')
    assert first.records[0]['content'] in ('Title','An abstract')


def test_native_filters_bounds_and_credentials():
    p=parameters('crossref',{'parameters':{'doi':'https://doi.org/10.1/example','rows':9000},'from_ms':0},cursor='resume',limit=2,contact='contact@example.org',secret='unused')
    assert p=={'filter':'doi:10.1/example,from-pub-date:1970-01-01','cursor':'resume','rows':2,'mailto':'contact@example.org'}
    p=parameters('openalex',{'parameters':{'author':'A1'},'to_ms':0},cursor=None,limit=1000,secret='private')
    assert p['per_page']==200 and p['filter']=='authorships.author.id:A1,to_publication_date:1970-01-01'
    assert p['api_key']=='private'


@pytest.mark.parametrize('name', ['crossref','openalex'])
def test_malformed_envelopes_fail(name):
    with pytest.raises(ValueError):records(name,{'data':[]},cursor=None,limit=1)
    payload={'message':{'items':[{},{}]}} if name=='crossref' else {'results':[{},{}]}
    with pytest.raises(ValueError):records(name,payload,cursor=None,limit=1)
