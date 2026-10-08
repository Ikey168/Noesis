"""Phase-3 bulk adapters (FA13–FA16) on offline fixtures."""
import gzip
import hashlib
import json

import pytest

from src.ingestion.bulk import get_bulk_adapter
from src.ingestion.bulk.base import FileSource, ReleaseFile
from src.ingestion.connectors.scholarly.base import _document_id
from tests.unit.ingestion.test_bulk_runner import FakeWeb, read_subset, runner


def test_openalex_snapshot_remote_query_on_local_parquet(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "part_0000.parquet"
    duckdb.sql(f"""COPY (SELECT * FROM (VALUES
        ('https://openalex.org/W1', 'https://doi.org/10.1000/abc', 'LLM tutors in college', DATE '2024-05-01', 2024, 'en',
         'article', [{{'author': {{'display_name': 'A. Author'}}}}], {{'source': {{'id': 'https://openalex.org/S1', 'display_name': 'Comp & Ed'}}}},
         {{'oa_url': 'https://x/pdf'}}, '{{"Large":[0],"models":[1]}}', {{'id': 'https://openalex.org/T1'}}, 3, false, DATE '2026-09-23'),
        ('https://openalex.org/W2', NULL, 'Unrelated', DATE '2010-01-01', 2010, 'de', 'article', [], NULL, NULL, NULL, NULL, 0, false, DATE '2026-09-23'))
        t(id, doi, display_name, publication_date, publication_year, language, type, authorships, primary_location,
          open_access, abstract_inverted_index, primary_topic, cited_by_count, is_retracted, updated_date))
        TO '{path}' (FORMAT PARQUET)""")
    adapter = get_bulk_adapter("openalex-snapshot")
    params = adapter.validate_params({"dois": ["https://doi.org/10.1000/ABC"]})

    class LocalHttp:
        def check(self, url):
            return "local"

    adapter._db = duckdb.connect()  # no httpfs needed for a local file
    docs = list(adapter.process(FileSource(ReleaseFile("p", str(path), mode="remote",
                                                       metadata={"partition": "2026-09-23"}), http=LocalHttp()), params))
    assert len(docs) == 1
    d = docs[0]
    assert d.content == "Large models" and d.authors == ["A. Author"] and d.metadata["venue"] == "Comp & Ed"
    assert d.document_id == _document_id("crossref", "x", "10.1000/abc")      # dedupes with other sources
    title = adapter.validate_params({"title_contains": "tutors", "publication_year_from": 2020})
    assert [x.title for x in adapter.process(FileSource(ReleaseFile("p", str(path), mode="remote"), http=LocalHttp()), title)] == ["LLM tutors in college"]


def test_openalex_manifest_and_updated_since(tmp_path):
    manifest = {"date": "2026-09-23", "record_count": 3, "files": [
        {"url": "s3://openalex/data/parquet/works/updated_date=2016-06-24/part_0000.parquet", "meta": {"content_length": 10, "record_count": 1}},
        {"url": "s3://openalex/data/parquet/works/updated_date=2026-09-23/part_0000.parquet", "meta": {"content_length": 20, "record_count": 2}}]}
    web = FakeWeb({"https://openalex.s3.amazonaws.com/data/parquet/works/manifest.json": (json.dumps(manifest).encode(), {})})
    r = runner(get_bulk_adapter("openalex-snapshot"), tmp_path, web).run(
        {"dois": ["10.1000/x"], "updated_since": "2026-01-01"}, dry_run=True)
    assert r["release_id"] == "openalex-works-2026-09-23"
    assert [f["name"] for f in r["files"]] == ["updated_date=2026-09-23/part_0000.parquet"]


PUBMED_XML = b"""<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID>111</PMID><Article><Journal><ISSN>0360-1315</ISSN><JournalIssue><PubDate><Year>2024</Year><Month>Mar</Month></PubDate></JournalIssue><Title>Computers &amp; Education</Title></Journal>
<ArticleTitle>ChatGPT and <i>student</i> achievement.</ArticleTitle><Abstract><AbstractText Label="BACKGROUND">Bg.</AbstractText><AbstractText Label="RESULTS">Gains.</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList><Language>eng</Language></Article>
<MeshHeadingList><MeshHeading><DescriptorName>Students</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1000/PM1</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>222</PMID><Article><ArticleTitle>Cardiology</ArticleTitle></Article></MedlineCitation></PubmedArticle>
<DeleteCitation><PMID>999</PMID></DeleteCitation></PubmedArticleSet>"""


def test_pubmed_md5_keywords_documents_and_deletions(tmp_path):
    gz = gzip.compress(PUBMED_XML)
    md5 = hashlib.md5(gz).hexdigest()  # noqa: S324
    base = "https://ftp.ncbi.nlm.nih.gov/pubmed/"
    listing = b'<a href="pubmed26n0001.xml.gz">pubmed26n0001.xml.gz</a>     2026-01-29 14:48   19M\n'
    web = FakeWeb({base + "baseline/": (listing, {}), base + "baseline/pubmed26n0001.xml.gz": (gz, {}),
                   base + "baseline/pubmed26n0001.xml.gz.md5": (f"MD5(pubmed26n0001.xml.gz)= {md5}\n".encode(), {})})
    r = runner(get_bulk_adapter("pubmed-baseline"), tmp_path, web).run({"keywords": ["chatgpt"], "include_updates": False})
    assert r["status"] == "complete" and r["release_id"] == "pubmed26-baseline"
    docs = read_subset(r, "documents")
    assert [d["metadata"]["external_id"] for d in docs] == ["111"]
    d = docs[0]
    assert d["title"] == "ChatGPT and student achievement" and d["content"] == "BACKGROUND: Bg.\nRESULTS: Gains."
    assert d["authors"] == ["Jane Doe"] and d["metadata"]["mesh"] == ["Students"]
    assert d["document_id"] == _document_id("x", "y", "10.1000/pm1")
    assert read_subset(r, "deletions") == [{"_file": "baseline/pubmed26n0001.xml.gz", "pmid": "999", "file": "baseline/pubmed26n0001.xml.gz"}]
    # wrong md5 -> quarantined
    web.files[base + "baseline/pubmed26n0001.xml.gz.md5"] = (b"MD5(x)= " + b"0" * 32, {})
    bad = runner(get_bulk_adapter("pubmed-baseline"), tmp_path / "b", web).run({"keywords": ["chatgpt"], "include_updates": False})
    assert bad["files"][0]["action"] == "quarantined"


def test_s2_datasets_with_key_and_without(tmp_path, monkeypatch):
    api = "https://api.semanticscholar.org/datasets/v1/"
    shard = "https://ai2-s2ag.s3.amazonaws.com/staging/2026-09-29/abstracts/0.gz?X-Amz-Signature=x"
    lines = "\n".join(json.dumps(r) for r in [
        {"corpusid": 1, "openaccessinfo": {"externalids": {"DOI": "10.1000/A"}, "license": "CCBY"}, "abstract": "abs one"},
        {"corpusid": 2, "openaccessinfo": {"externalids": {}}, "abstract": "abs two"}])
    web = FakeWeb({api + "release/latest": (b'{"release_id": "2026-09-29"}', {}),
                   api + "release/2026-09-29/dataset/abstracts": (json.dumps({"files": [shard]}).encode(), {}),
                   shard: (gzip.compress(lines.encode()), {})})
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    missing = runner(get_bulk_adapter("s2-datasets"), tmp_path / "nokey", web).run({"dois": ["10.1000/a"]})
    assert missing["status"] == "failed" and "SEMANTIC_SCHOLAR_API_KEY" in missing["errors"][0]["error"]
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "S2KEY")
    r = runner(get_bulk_adapter("s2-datasets"), tmp_path, web).run({"dois": ["10.1000/a"]})
    assert [x["corpusid"] for x in read_subset(r, "abstracts")] == [1]
    sent = {url: hdr for _, url, hdr in web.requests}
    assert sent[api + "release/2026-09-29/dataset/abstracts"].get("X-api-key") == "S2KEY"
    assert "X-api-key" not in sent[shard]                                # key never sent to S3


def test_openaq_archive_lists_days_and_filters_parameters(tmp_path):
    bucket = "https://openaq-data-archive.s3.amazonaws.com/"
    key = "records/csv.gz/locationid=2178/year=2025/month=01/location-2178-20250102.csv.gz"
    listing = (f"<ListBucketResult><Contents><Key>records/csv.gz/locationid=2178/year=2025/month=01/location-2178-20250101.csv.gz</Key>"
               f"<LastModified>2025-01-02T00:00:00Z</LastModified><ETag>&quot;a&quot;</ETag><Size>10</Size></Contents>"
               f"<Contents><Key>{key}</Key><LastModified>2025-01-03T00:00:00Z</LastModified><ETag>&quot;b&quot;</ETag>"
               f"<Size>10</Size></Contents></ListBucketResult>").encode()
    csv = ('"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"\n'
           '2178,3919,"Del Norte","2025-01-02T01:00:00-07:00","35.1","-106.5","pm10","µg/m³","9.0"\n'
           '2178,3920,"Del Norte","2025-01-02T01:00:00-07:00","35.1","-106.5","o3","ppm","0.03"\n').encode()
    web = FakeWeb({bucket + "?list-type=2&prefix=records%2Fcsv.gz%2Flocationid%3D2178%2Fyear%3D2025%2Fmonth%3D01%2F": (listing, {}),
                   bucket + key: (gzip.compress(csv), {})})
    r = runner(get_bulk_adapter("openaq-archive"), tmp_path, web).run(
        {"location_ids": [2178], "date_from": "2025-01-02", "date_to": "2025-01-02", "parameters": ["pm10"]})
    rows = read_subset(r, "measurements")
    assert len(r["files"]) == 1 and [(x["parameter"], x["value"]) for x in rows] == [("pm10", 9.0)]
