"""
arXiv discovery and metadata parsing.

Fetches paper metadata from the arXiv Atom API and parses it into
``PaperMetadata``. The HTTP layer is injectable so callers (and tests) can
supply recorded responses instead of hitting the network.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Callable, List, Optional
import xml.etree.ElementTree as ET

from src.ingestion.connectors.paper.models import PaperMetadata

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"

ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "NeuroNewsBot/1.0 (+https://github.com/Ikey168/NeuroNews)"
HTTP_TIMEOUT = 20

HttpGet = Callable[[str], bytes]


def _default_http_get(url: str) -> bytes:
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _strip_version(arxiv_id: str) -> str:
    """Drop a trailing version suffix, e.g. ``1706.03762v7`` -> ``1706.03762``."""
    return re.sub(r"v\d+$", "", arxiv_id.strip())


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_atom(xml_bytes: bytes) -> List[PaperMetadata]:
    """Parse an arXiv Atom API response into one PaperMetadata per entry."""
    root = ET.fromstring(xml_bytes)
    papers: List[PaperMetadata] = []

    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        arxiv_id = None
        if "/abs/" in raw_id:
            arxiv_id = _strip_version(raw_id.rsplit("/abs/", 1)[1])

        title = re.sub(r"\s+", " ", (entry.findtext(f"{_ATOM}title") or "").strip())
        abstract = re.sub(r"\s+", " ", (entry.findtext(f"{_ATOM}summary") or "").strip())
        authors = [
            (a.findtext(f"{_ATOM}name") or "").strip()
            for a in entry.findall(f"{_ATOM}author")
            if (a.findtext(f"{_ATOM}name") or "").strip()
        ]

        doi = entry.findtext(f"{_ARXIV}doi")
        primary_el = entry.find(f"{_ARXIV}primary_category")
        primary_category = primary_el.get("term") if primary_el is not None else None
        categories = [
            c.get("term") for c in entry.findall(f"{_ATOM}category") if c.get("term")
        ]

        pdf_url = None
        for link in entry.findall(f"{_ATOM}link"):
            if link.get("type") == "application/pdf" or link.get("title") == "pdf":
                pdf_url = link.get("href")
                break

        papers.append(
            PaperMetadata(
                title=title,
                version_id=raw_id.rsplit("/abs/",1)[-1] if "/abs/" in raw_id else None,
                updated=_parse_date(entry.findtext(f"{_ATOM}updated")),
                arxiv_id=arxiv_id,
                doi=doi.strip() if doi else None,
                authors=authors,
                abstract=abstract,
                categories=categories,
                primary_category=primary_category,
                published=_parse_date(entry.findtext(f"{_ATOM}published")),
                pdf_url=pdf_url,
            )
        )
    return papers


class ArxivClient:
    """Thin client for fetching arXiv metadata by id."""

    def __init__(self, http_get: Optional[HttpGet] = None, api_url: str = ARXIV_API):
        self._http_get = http_get or _default_http_get
        self._api_url = api_url

    def fetch_by_id(self, arxiv_id: str) -> bytes:
        url = self._api_url + "?" + urlencode({"id_list":arxiv_id.strip(), "max_results":1})
        return self._http_get(url)

    def search(self, objective, *, sleep=time.sleep):
        limit = int(objective.get('limit', 20))
        pages = int(objective.get('max_pages', 3))
        page_size = int(objective.get('page_size', 20))
        if not 1 <= limit <= 1000 or not 1 <= pages <= 10 or not 1 <= page_size <= 100:
            raise ValueError('arXiv discovery budget exceeded')
        terms = []
        for field, prefix in [('topic','all'), ('author','au')]:
            value = objective.get(field)
            if value:
                if not isinstance(value,str) or len(value)>1000 or '"' in value:
                    raise ValueError('invalid arXiv search term')
                terms.append(prefix + ':"' + value + '"')
        if objective.get('from_date') or objective.get('to_date'):
            start = str(objective.get('from_date','1900-01-01')).replace('-','')
            end = str(objective.get('to_date','2999-12-31')).replace('-','')
            if not re.fullmatch(r'\d{8}',start) or not re.fullmatch(r'\d{8}',end) or start>end:
                raise ValueError('invalid arXiv date interval')
            terms.append('submittedDate:[' + start + '0000 TO ' + end + '2359]')
        if not terms:
            raise ValueError('explicit scholarly search objective required')
        seen = set()
        start = int(objective.get('start',0))
        if start<0 or start>10000:raise ValueError('invalid arXiv start')
        for page in range(pages):
            if page:sleep(3)
            size = min(page_size,limit-len(seen))
            params={'search_query':' AND '.join(terms),'start':start,'max_results':size,'sortBy':'submittedDate','sortOrder':'descending'}
            papers=parse_atom(self._http_get(self._api_url+'?'+urlencode(params)))
            if len(papers)>size:raise ValueError('arXiv page exceeded result budget')
            for paper in papers:
                identity=paper.version_id or paper.arxiv_id
                if not identity:raise ValueError('arXiv result missing identifier')
                if identity not in seen:
                    seen.add(identity)
                    yield paper
            if len(papers)<size or len(seen)>=limit:return
            start += size

    def get_metadata(self, arxiv_id: str) -> PaperMetadata:
        papers = parse_atom(self.fetch_by_id(arxiv_id))
        if not papers:
            raise ValueError(f"No arXiv entry found for {arxiv_id!r}")
        return papers[0]
