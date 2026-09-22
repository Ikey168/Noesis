"""Persistent feed validators and bounded transport; 304 is an unchanged check."""
import sqlite3
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
from src.ingestion.connectors.base import PermanentFetchError


class RetryableHTTPError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f'HTTP {status}')
        self.retry_after = retry_after


def retry_after_seconds(value, now=None):
    try:
        seconds=float(value)
    except (ValueError,TypeError):
        try: seconds=parsedate_to_datetime(value).timestamp()-(time.time() if now is None else now)
        except (ValueError,TypeError,OverflowError):return None
    return max(0,seconds) if seconds<86400 else 86400


def get_response(url, headers):
    try:
        response=urlopen(Request(url,headers=headers),timeout=15)
    except HTTPError as error:
        if error.code==304:
            error.close();return 304,dict(error.headers),b''
        if error.code in (408,429,500,502,503,504):
            retry=retry_after_seconds(error.headers.get('Retry-After'))
            error.close();raise RetryableHTTPError(error.code,retry) from None
        error.close();raise PermanentFetchError(f'HTTP {error.code}') from None
    with response:
        data=response.read(5_000_001)
        if len(data)>5_000_000:raise PermanentFetchError('feed response exceeds byte budget')
        return response.status,dict(response.headers),data


class FeedHTTPStore:
    def __init__(self,path,transport=None):
        self.path=Path(path)
        self.transport=transport or get_response

    def fetch(self,url):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS feeds(url TEXT PRIMARY KEY, etag TEXT, modified TEXT, body BLOB, fetched INTEGER, checked INTEGER)')
            old=conn.execute('SELECT etag,modified,body,fetched,checked FROM feeds WHERE url=?',(url,)).fetchone()
        headers={'User-Agent':'Noesis/1.0','Accept':'application/rss+xml,application/atom+xml,application/xml'}
        if old and old[0]:headers['If-None-Match']=old[0]
        if old and old[1]:headers['If-Modified-Since']=old[1]
        status,response_headers,body=self.transport(url,headers)
        now=int(time.time()*1000)
        if status==304:
            if old is None:raise ValueError('304 without a persisted feed representation')
            with sqlite3.connect(self.path) as conn:conn.execute('UPDATE feeds SET checked=? WHERE url=?',(now,url))
            return b'',{'outcome':'unchanged','original_fetched_at':old[3],'revalidated_at':now}
        if status!=200:raise RetryableHTTPError(status)
        h={k.lower():v for k,v in response_headers.items()}
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT OR REPLACE INTO feeds VALUES(?,?,?,?,?,?)',(url,h.get('etag'),h.get('last-modified'),body,now,now))
        return body,{'outcome':'fetched','original_fetched_at':now,'revalidated_at':now}
