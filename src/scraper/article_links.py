"""Stable, source-scoped article discovery from already fetched HTML."""
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup


def scoped_url(base, value):
    absolute = urlsplit(urljoin(base, value))
    origin = urlsplit(base)
    if absolute.scheme not in {'http', 'https'} or absolute.hostname != origin.hostname or absolute.username or absolute.password:
        return None
    return urlunsplit((absolute.scheme, absolute.netloc, absolute.path, absolute.query, ''))


def extract_links(html, source):
    if not isinstance(html, str) or not html.strip() or '<' not in html:
        raise ValueError('discovery_invalid_html')
    soup = BeautifulSoup(html, 'html.parser')
    patterns = [re.compile(pattern) for pattern in source.link_patterns]
    links = []
    for anchor in soup.select('a[href]'):
        url = scoped_url(source.base_url, anchor['href'])
        if url and (not patterns or any(pattern.search(urlsplit(url).path) for pattern in patterns)) and url not in links:
            links.append(url)
    return links
