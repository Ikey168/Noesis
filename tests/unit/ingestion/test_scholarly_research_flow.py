import asyncio
import json
import urllib.error

import duckdb
import pytest

from src.ingestion.connectors.scholarly.base import _document_id
from services.ingest.common.document_model import Document
from src.ingestion.connectors.scholarly.sources import (
    CrossrefConnector,
    OpenalexConnector,
    SemanticScholarConnector,
)
from tools.pipeline_mcp import server


def _crossref_payload():
    return json.dumps({
        "message": {
            "items": [{
                "DOI": "10.1007/978-3-642-22816-2_10",
                "title": ["Ternary Computers: The Setun and the Setun 70"],
                "published": {"date-parts": [[2011, 1, 1]]},
                "URL": "https://doi.org/10.1007/978-3-642-22816-2_10",
                "abstract": "A history of the Setun ternary computers at Moscow State University.",
            }]
        }
    }).encode()


def test_crossref_search_ranks_relevance_and_keeps_conference_papers():
    url = next(CrossrefConnector().discover({
        "topic": "Setun ternary computer",
        "since": "2010-01-01",
        "until": "2026-09-23",
        "limit": 10,
    })).locator
    assert "query.bibliographic=Setun%20ternary%20computer" in url
    assert "sort=score&order=desc" in url
    assert "type:journal-article" not in url
    openalex_url = next(OpenalexConnector().discover({"topic": "Setun", "limit": 10})).locator
    assert "per_page=10" in openalex_url
    assert "sort=publication_date" not in openalex_url


def test_openalex_restores_indexed_abstract_and_zenodo_searches_by_relevance():
    from src.ingestion.connectors.scholarly.sources import ZenodoConnector

    payload = json.dumps({"results": [{
        "id": "https://openalex.org/W123",
        "display_name": "Setun ternary computer",
        "publication_date": "2021-03-12",
        "abstract_inverted_index": {
            "Setun": [0], "used": [1], "balanced": [2], "ternary": [3], "logic.": [4]
        },
    }]}).encode()
    connector = OpenalexConnector(
        http_get=lambda url, headers: payload,
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    ref = next(connector.discover({"topic": "Setun ternary computer", "limit": 10}))
    document = connector.parse(connector.fetch(ref))[0]
    assert document.content == "Setun used balanced ternary logic."
    assert document.metadata["content_coverage"] == "abstract-only"
    assert "sort=bestmatch" in next(ZenodoConnector().discover("Setun")).locator


@pytest.mark.parametrize("connector_type,env_name", [
    (OpenalexConnector, "OPENALEX_API_KEY"),
    (SemanticScholarConnector, "SEMANTIC_SCHOLAR_API_KEY"),
])
def test_optional_scholarly_keys_are_sent_when_configured(monkeypatch, connector_type, env_name):
    monkeypatch.setenv(env_name, "test-key")
    observed = {}

    def fetch(url, headers):
        observed["url"] = url
        observed["headers"] = headers
        return b"{}"

    connector = connector_type(http_get=fetch, dns_resolver=lambda _: ["1.1.1.1"])
    connector.fetch(next(connector.discover("ternary computer")))
    if connector_type is OpenalexConnector:
        assert observed["headers"]["Authorization"] == "Bearer test-key"
        assert "test-key" not in observed["url"]
    else:
        assert observed["headers"]["x-api-key"] == "test-key"
        assert "test-key" not in observed["url"]


def test_rate_limit_is_explicit_and_doi_ids_match_across_providers(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)

    def rate_limited(url, headers):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    connector = OpenalexConnector(http_get=rate_limited, dns_resolver=lambda _: ["1.1.1.1"])
    with pytest.raises(Exception, match="OPENALEX_API_KEY"):
        connector.fetch(next(connector.discover("ternary computer")))

    assert _document_id("openalex", "W1", "https://doi.org/10.1234/ABC") == _document_id(
        "crossref", "10.1234/abc", "10.1234/abc"
    )


def test_openalex_http_error_does_not_reveal_api_key(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "private-test-key")

    def failed(url, headers):
        raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)

    connector = OpenalexConnector(http_get=failed, dns_resolver=lambda _: ["1.1.1.1"])
    with pytest.raises(Exception, match="openalex returned HTTP 503") as error:
        connector.fetch(next(connector.discover("Setun")))
    assert "private-test-key" not in str(error.value)


def test_year_only_publication_date_is_retained_when_window_overlaps():
    payload = json.loads(_crossref_payload())
    payload["message"]["items"][0]["published"] = {"date-parts": [[2011]]}
    connector = CrossrefConnector(
        http_get=lambda url, headers: json.dumps(payload).encode(),
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    ref = next(connector.discover({
        "topic": "Setun", "since": "2011-07-01", "until": "2011-12-31",
    }))
    documents = connector.parse(connector.fetch(ref))
    assert len(documents) == 1
    assert documents[0].metadata["publication_date_precision"] == "year"


def test_crossref_title_decodes_nested_html_entities():
    payload = json.loads(_crossref_payload())
    payload["message"]["items"][0]["title"] = [
        "Software for a Small Computer &amp;#x0022;Setun&amp;#x0022;"
    ]
    connector = CrossrefConnector(
        http_get=lambda url, headers: json.dumps(payload).encode(),
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    ref = next(connector.discover("Setun"))
    documents = connector.parse(connector.fetch(ref))
    assert documents[0].title == 'Software for a Small Computer "Setun"'


def test_crossref_keeps_reference_and_citation_metadata_separate():
    payload = json.loads(_crossref_payload())
    item = payload["message"]["items"][0]
    item["abstract"] = "<jats:p>Setun used &lt;three&gt; states.</jats:p>"
    item["container-title"] = ["Perspectives on Soviet and Russian Computing"]
    item["reference"] = [{"DOI": "10.1000/earlier"}, {"key": "unresolved"}]
    item["is-referenced-by-count"] = 0
    connector = CrossrefConnector(
        http_get=lambda url, headers: json.dumps(payload).encode(),
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    document = connector.parse(connector.fetch(next(connector.discover("Setun"))))[0]
    assert document.content == "Setun used <three> states."
    assert document.metadata["venue"] == "Perspectives on Soviet and Russian Computing"
    assert document.metadata["references"] == ["10.1000/earlier"]
    assert document.metadata["citations"] == 0


def test_harvest_scholarly_previews_then_ingests_papers(tmp_path, monkeypatch):
    db_path = tmp_path / "papers.duckdb"
    duckdb.connect(str(db_path)).close()
    monkeypatch.setattr(server, "_db_path", lambda: str(db_path))

    connector = CrossrefConnector(
        http_get=lambda url, headers: _crossref_payload(),
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    monkeypatch.setattr(
        "src.ingestion.connectors.registry.get_connector", lambda _: connector
    )
    tool = asyncio.run(server.mcp.get_tools())["harvest_scholarly"].fn
    args = {
        "source": "crossref",
        "topic": "Setun ternary computer",
        "since": "2010-01-01",
        "until": "2026-09-23",
    }

    preview = tool(**args)
    assert preview["harvested"] == 1
    assert preview["applied"] is False
    assert tool(**args, apply=True)["error"] == "apply requires selected_document_ids from a preview"
    assert duckdb.connect(str(db_path), read_only=True).execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='documents'"
    ).fetchone()[0] == 0

    applied = tool(**args, apply=True, selected_document_ids=[preview["documents"][0]["document_id"]])
    assert applied["upsert"]["inserted"] == 1
    assert applied["applied"] is True
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM documents WHERE source_type='paper'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM document_domains WHERE domain='papers'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_scholarly_merge_keeps_abstract_and_records_conflicting_dates(tmp_path, monkeypatch):
    db_path = tmp_path / "merged-papers.duckdb"
    duckdb.connect(str(db_path)).close()
    monkeypatch.setattr(server, "_db_path", lambda: str(db_path))
    crossref = CrossrefConnector(
        http_get=lambda url, headers: _crossref_payload(),
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    openalex_payload = json.dumps({"results": [{
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1007/978-3-642-22816-2_10",
        "display_name": "Ternary Computers: The Setun and the Setun 70",
        "publication_date": "2006-01-01",
    }]}).encode()
    openalex = OpenalexConnector(
        http_get=lambda url, headers: openalex_payload,
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    monkeypatch.setattr(
        "src.ingestion.connectors.registry.get_connector",
        lambda source: {"crossref": crossref, "openalex": openalex}[source],
    )
    tool = asyncio.run(server.mcp.get_tools())["harvest_scholarly"].fn
    args = {"topic": "Setun ternary computer", "since": "2000-01-01", "until": "2026-09-23"}

    rich = tool(source="crossref", **args)
    document_id = rich["documents"][0]["document_id"]
    assert tool(source="crossref", **args, apply=True, selected_document_ids=[document_id])["upsert"]["inserted"] == 1
    sparse = tool(source="openalex", **args)
    assert sparse["documents"][0]["document_id"] == document_id
    applied = tool(source="openalex", **args, apply=True, selected_document_ids=[document_id])
    assert applied["upsert"]["updated"] == 1

    from src.ingestion.document_store import DocumentStore

    con = duckdb.connect(str(db_path))
    try:
        stored = DocumentStore(con).get(document_id)
        assert stored["content"] == "A history of the Setun ternary computers at Moscow State University."
        assert stored["source_id"] == "crossref"
        assert stored["metadata"]["content_coverage"] == "abstract-only"
        assert stored["metadata"]["publication_date_conflict"] is True
        assert {item["publication_date"] for item in json.loads(stored["metadata"]["scholarly_observations_json"])} == {
            "2011-01-01", "2006-01-01",
        }
    finally:
        con.close()


def test_scholarly_merge_respects_publication_date_precision():
    existing = Document(
        document_id="paper:test", source_type="paper", language="en", ingested_at=1,
        source_id="crossref", title="Setun", content="Abstract", created_at=1293840000000,
        metadata={"publication_date_precision": "year", "content_coverage": "abstract-only"},
    )
    incoming = Document(
        document_id="paper:test", source_type="paper", language="en", ingested_at=2,
        source_id="openalex", title="Setun", created_at=1318464000000,
        metadata={"publication_date_precision": "day", "content_coverage": "metadata-only"},
    )
    merged = server._merge_scholarly_observation(incoming, existing.to_dict())
    assert merged.metadata["publication_date_conflict"] is False


def test_harvest_filters_unrelated_provider_hits_and_reports_evidence_gap(monkeypatch):
    payload = json.dumps({"message": {"items": [
        {
            "DOI": "10.1000/unrelated",
            "title": ["Pea crop yield in a greenhouse"],
            "published": {"date-parts": [[2023, 1, 1]]},
        },
        {
            "DOI": "10.1000/setun",
            "title": ["Ternary Computers: The Setun and the Setun 70"],
            "published": {"date-parts": [[2011]]},
        },
    ]}}).encode()
    requested_urls = []
    def get_payload(url, headers):
        requested_urls.append(url)
        return payload
    connector = CrossrefConnector(
        http_get=get_payload,
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    monkeypatch.setattr("src.ingestion.connectors.registry.get_connector", lambda _: connector)
    tool = asyncio.run(server.mcp.get_tools())["harvest_scholarly"].fn
    preview = tool(
        source="crossref", topic="Setun ternary computer Soviet",
        since="2010-01-01", until="2026-09-23", limit=1,
    )
    assert preview["harvested"] == 1
    assert preview["provider_harvested"] == 2
    assert preview["filtered_irrelevant"] == 1
    assert preview["metadata_only_count"] == 1
    assert "metadata only" in preview["coverage_warning"]
    assert "rows=25" in requested_urls[0]


def test_harvest_requires_named_topic_anchor_before_generic_terms(monkeypatch):
    payload = json.dumps({"message": {"items": [
        {
            "DOI": "10.1000/controller",
            "title": ["Hydraulic integrator controller for a cylinder"],
            "published": {"date-parts": [[2021, 1, 1]]},
        },
        {
            "DOI": "10.1109/ent.2018.00019",
            "title": ["Lukyanov's Hydraulic Integrator and Moor's Hydrocal"],
            "published": {"date-parts": [[2018, 3]]},
        },
    ]}}).encode()
    connector = CrossrefConnector(
        http_get=lambda url, headers: payload,
        dns_resolver=lambda _: ["1.1.1.1"],
    )
    monkeypatch.setattr("src.ingestion.connectors.registry.get_connector", lambda _: connector)
    tool = asyncio.run(server.mcp.get_tools())["harvest_scholarly"].fn
    preview = tool(
        source="crossref", topic="Lukyanov hydraulic integrator",
        since="2000-01-01", until="2026-09-23", limit=2,
    )
    assert [item["title"] for item in preview["documents"]] == [
        "Lukyanov's Hydraulic Integrator and Moor's Hydrocal"
    ]
    assert preview["filtered_irrelevant"] == 1
