"""Abstract backfill by DOI: provider order, batching, provenance, failures (offline)."""
import json
import urllib.error
import urllib.parse

import pytest

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import PermanentFetchError
from src.ingestion.connectors.scholarly.abstracts import (
    OPENALEX_BATCH,
    AbstractBackfill,
    normalise_doi,
    rebuild_inverted_abstract,
    strip_jats,
)

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def inverted(text):
    index = {}
    for i, word in enumerate(text.split()):
        index.setdefault(word, []).append(i)
    return index


class FakeWeb:
    """Serves OpenAlex, Crossref and Semantic Scholar from dicts keyed by DOI."""

    def __init__(self, openalex=None, crossref=None, s2=None, fail=()):
        self.openalex, self.crossref, self.s2 = openalex or {}, crossref or {}, s2 or {}
        self.fail = set(fail)
        self.calls = []

    def get(self, url, headers):
        parsed = urllib.parse.urlparse(url)
        self.calls.append(("GET", parsed.hostname, url, dict(headers)))
        if parsed.hostname == "api.openalex.org":
            if "openalex" in self.fail:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            wanted = params["filter"].removeprefix("doi:").split("|")
            results = [{"id": f"https://openalex.org/W{i}", "doi": "https://doi.org/" + d,
                        "abstract_inverted_index": inverted(self.openalex[d])}
                       for i, d in enumerate(wanted) if d in self.openalex]
            return json.dumps({"results": results}).encode()
        if parsed.hostname == "api.crossref.org":
            if "crossref" in self.fail:
                raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
            doi = urllib.parse.unquote(parsed.path.removeprefix("/works/"))
            if doi not in self.crossref:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps({"message": {"abstract": self.crossref[doi]}}).encode()
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        self.calls.append(("POST", parsed.hostname, url, dict(headers)))
        ids = json.loads(body)["ids"]
        out = []
        for i in ids:
            doi = i.removeprefix("DOI:")
            out.append({"paperId": "S" + doi[-3:], "abstract": self.s2[doi]} if doi in self.s2 else None)
        return json.dumps(out).encode()


def backfill(web, **kw):
    return AbstractBackfill(http_get=web.get, http_post=web.post, dns_resolver=PUBLIC,
                            contact="team@example.org", **kw)


def test_provider_order_and_provenance():
    web = FakeWeb(openalex={"10.1000/a": "alpha text"},
                  crossref={"10.1000/b": "<jats:title>Abstract</jats:title><jats:p>beta text</jats:p>"},
                  s2={"10.1000/c": "gamma text"})
    report = backfill(web).fetch(["10.1000/a", "https://doi.org/10.1000/B", "doi:10.1000/c", "10.1000/d"])
    r = report["results"]
    assert r["10.1000/a"]["abstract"] == "alpha text" and r["10.1000/a"]["provider"] == "openalex"
    assert r["10.1000/b"]["abstract"] == "beta text" and r["10.1000/b"]["provider"] == "crossref"
    assert r["10.1000/c"]["abstract"] == "gamma text" and r["10.1000/c"]["provider"] == "semantic_scholar"
    assert report["missing"] == ["10.1000/d"]
    assert report["found"] == 3 and report["requested"] == 4
    # Crossref is only asked for what OpenAlex did not have; S2 only for the rest.
    crossref_dois = [urllib.parse.unquote(c[2].split("/works/")[1].split("?")[0])
                     for c in web.calls if c[1] == "api.crossref.org"]
    assert crossref_dois == ["10.1000/b", "10.1000/c", "10.1000/d"]


def test_openalex_batches_and_contact():
    dois = [f"10.1000/{i:03d}" for i in range(OPENALEX_BATCH + 5)]
    web = FakeWeb(openalex={d: "x y" for d in dois})
    report = backfill(web).fetch(dois, providers=("openalex",))
    assert report["found"] == len(dois)
    assert report["requests"]["openalex"] == 2
    assert all("mailto=team%40example.org" in c[2] for c in web.calls)


def test_failing_provider_is_recorded_and_next_one_used():
    web = FakeWeb(crossref={"10.1000/a": "<p>from crossref</p>"}, fail={"openalex"})
    report = backfill(web).fetch(["10.1000/a"])
    assert report["errors"] == {"openalex": "HTTP 429"}
    assert report["results"]["10.1000/a"]["provider"] == "crossref"


def test_invalid_dois_and_unknown_provider():
    report = backfill(FakeWeb()).fetch(["not-a-doi", "", "10.1000/x"], providers=("crossref",))
    assert report["invalid"] == ["not-a-doi", ""]
    assert report["missing"] == ["10.1000/x"]
    with pytest.raises(ValueError):
        backfill(FakeWeb()).fetch(["10.1000/x"], providers=("scopus",))


def test_fill_copies_documents_and_keeps_existing_content():
    web = FakeWeb(openalex={"10.1000/a": "alpha text"})
    empty = Document(document_id="paper:1", source_type="paper", language="en", ingested_at=1,
                     source_id="scopus", title="A", content=None,
                     metadata={"doi": "10.1000/A", "content_coverage": "metadata-only"})
    has = Document(document_id="paper:2", source_type="paper", language="en", ingested_at=1,
                   source_id="openalex", title="B", content="own abstract", metadata={"doi": "10.1000/b"})
    nodoi = Document(document_id="paper:3", source_type="paper", language="en", ingested_at=1,
                     source_id="scopus", title="C", content=None, metadata={})
    filled, report = backfill(web).fill([empty, has, nodoi])
    assert filled[0].content == "alpha text"
    assert filled[0].metadata["abstract_source"] == "openalex"
    assert filled[0].metadata["content_coverage"] == "abstract-only"
    assert empty.content is None and empty.metadata["content_coverage"] == "metadata-only"
    assert filled[1] is has and filled[2] is nodoi
    assert report["requested"] == 1


def test_keys_sent_as_documented():
    web = FakeWeb(s2={"10.1000/a": "s2 text"})
    backfill(web, openalex_key="OAKEY", semantic_scholar_key="S2KEY").fetch(["10.1000/a"])
    openalex = [c for c in web.calls if c[1] == "api.openalex.org"][0]
    s2 = [c for c in web.calls if c[1] == "api.semanticscholar.org"][0]
    assert "api_key=OAKEY" in openalex[2]
    assert s2[3]["x-api-key"] == "S2KEY"


def test_private_address_is_refused():
    web = FakeWeb()
    client = AbstractBackfill(http_get=web.get, http_post=web.post, dns_resolver=lambda h: ["10.0.0.5"])
    report = client.fetch(["10.1000/a"], providers=("crossref",))
    assert "non-public address" in report["errors"]["crossref"]
    assert report["missing"] == ["10.1000/a"] and web.calls == []
    with pytest.raises(PermanentFetchError):
        client._crossref(["10.1000/a"], {"crossref": 0})


def test_helpers():
    assert normalise_doi("https://doi.org/10.1234/ABC") == "10.1234/abc"
    assert normalise_doi("10.1234") is None
    assert rebuild_inverted_abstract({"b": [1], "a": [0]}) == "a b"
    with pytest.raises(ValueError):
        rebuild_inverted_abstract({"a": [0], "b": [2]})
    assert strip_jats("<jats:title>Abstract</jats:title><jats:p>Hello <i>world</i></jats:p>") == "Hello world"
