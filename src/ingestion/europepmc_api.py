"""Europe PMC core metadata mapping for the bounded source-pack HTTP adapter."""
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit


def is_europepmc(source):
    endpoint=urlsplit(source['endpoint'])
    return source['source_id']=='europe-pmc' and endpoint.hostname=='www.ebi.ac.uk' and endpoint.path.rstrip('/')=='/europepmc/webservices/rest/search'


def parameters(values, *, cursor, limit):
    # Reserved controls cannot override the source-pack budget or response shape.
    result={k:v for k,v in values.items() if k not in {'cursor','cursorMark','limit','pageSize','resultType','format'}}
    result.update(format='json',resultType='core',pageSize=limit,cursorMark=cursor or '*')
    return result


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
    def handle_data(self,value):
        self.parts.append(value)
    def handle_starttag(self,tag,attrs):
        if tag in {'p','h4','br','div'}:
            self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'p','h4','div'}:
            self.parts.append('\n')


def records(payload, *, cursor, limit):
    if not isinstance(payload,dict) or not isinstance(payload.get('resultList'),dict) or not isinstance(payload['resultList'].get('result'),list):
        raise ValueError('Europe PMC response lacks resultList.result')
    values=payload['resultList']['result']
    if len(values)>limit:
        raise ValueError('Europe PMC returned more records than the requested page budget')
    mapped=[]
    for item in values:
        if not isinstance(item,dict) or not item.get('id') or not item.get('source'):
            raise ValueError('Europe PMC result lacks source and id')
        abstract=item.get('abstractText') or ''
        parser=_Text(); parser.feed(abstract); parser.close()
        body=''.join(parser.parts).strip()
        title=unescape(str(item.get('title') or ''))
        types=item.get('pubTypeList',{}).get('pubType',[])
        mapped.append({**item,'id':str(item['source'])+':'+str(item['id']),
            'url':'https://europepmc.org/article/'+str(item['source'])+'/'+str(item['id']),
            'title':title,'content':body or title,'abstract_available':bool(body),
            'content_representation':'plain-text-abstract' if body else 'title-only',
            'retracted':any(str(value).casefold()=='retracted publication' for value in types),
            'authors':[a['fullName'] for a in item.get('authorList',{}).get('author',[]) if a.get('fullName')],
            'published_at':item.get('firstPublicationDate'), 'europepmc_original_id':item['id']})
    next_cursor=payload.get('nextCursorMark')
    if not values or next_cursor==cursor or len(values)<limit or cursor is None and int(payload.get('hitCount',len(values)))<=len(values):
        next_cursor=None
    return mapped,next_cursor
