import json
import duckdb
import pytest
from src.ingestion.connectors.paper.full_text import (
    resolve_unpaywall,
    parse_jats,
    FullTextAcquirer,
)
from src.ingestion.snapshots import SnapshotStore

XML = b'<article><body><sec id="s1"><title>Results</title><p id="p1">Evidence <xref ref-type="bibr" rid="r1">1</xref>.</p></sec></body><back><ref-list><ref id="r1">A citation</ref></ref-list></back></article>'


def test_unpaywall_versions_unknown_license_and_missing_copy():
    def transport(**kw):
        assert kw["params"] == {"email": "research@example.org"}
        return {
            "content": json.dumps(
                {
                    "oa_locations": [
                        {
                            "url_for_pdf": "https://example.org/paper",
                            "version": "acceptedVersion",
                            "license": None,
                        }
                    ]
                }
            )
        }

    locations = resolve_unpaywall(
        "10.1234/test", contact="research@example.org", transport=transport
    )
    assert (
        locations[0]["license"] is None and locations[0]["version"] == "acceptedVersion"
    )
    assert (
        resolve_unpaywall(
            "10.1234/test",
            contact="research@example.org",
            transport=lambda **kw: {"content": '{"oa_locations":[]}'},
        )
        == []
    )
    with pytest.raises(ValueError):
        resolve_unpaywall("10.1234/test", contact="", transport=transport)


def test_jats_snapshot_repeat_change_and_failed_preserves_no_false_success():
    conn = duckdb.connect()
    bodies = [XML, XML, XML.replace(b"Evidence", b"Changed")]
    acquirer = FullTextAcquirer(
        SnapshotStore(conn),
        allowed_hosts=["www.ebi.ac.uk"],
        transport=lambda **kw: {
            "content": bodies.pop(0),
            "headers": {"Content-Type": "application/xml"},
        },
    )
    first = acquirer.europe_pmc("PMC123")
    assert first["outcome"] == "full-text" and first["sections"][0]["citations"] == [
        "r1"
    ]
    assert first["references"][0]["id"] == "r1"
    second = acquirer.europe_pmc("PMC123")
    third = acquirer.europe_pmc("PMC123")
    assert (
        first["snapshot"]["digest"]
        == second["snapshot"]["digest"]
        != third["snapshot"]["digest"]
    )
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone()[0] == 2
    acquirer.transport = lambda **kw: {"content": b"invalid"}
    assert acquirer.europe_pmc("PMC123")["outcome"] == "failed"
    acquirer.transport = lambda **kw: {"content": XML}
    acquirer.max_bytes = 2
    assert acquirer.europe_pmc("PMC123")["outcome"] == "failed"
    with pytest.raises(Exception):
        parse_jats(b"<article>")
    assert acquirer.acquire("https://outside.org/paper")["outcome"] == "failed"


def test_arxiv_structured_discovery_paces_pages_and_preserves_version():
    from urllib.parse import urlsplit, parse_qs
    from src.ingestion.connectors.paper.arxiv import ArxivClient
    from src.ingestion.connectors.paper.connector import PaperConnector

    urls = []
    pauses = []

    def get(url):
        urls.append(url)
        page = parse_qs(urlsplit(url).query).get("start", ["0"])[0]
        if page == "2":
            return b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        return (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2401.0000'
            + page
            + "v2</id><title>Title</title><summary>Abstract</summary></entry></feed>"
        ).encode()

    client = ArxivClient(http_get=get)
    papers = list(
        client.search(
            {
                "topic": "climate change",
                "author": "A B",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "limit": 3,
                "page_size": 1,
            },
            sleep=pauses.append,
        )
    )
    assert len(papers) == 2 and papers[0].version_id.endswith("v2") and pauses == [3, 3]
    query = parse_qs(urlsplit(urls[0]).query)["search_query"][0]
    assert (
        'all:"climate change"' in query
        and 'au:"A B"' in query
        and "submittedDate:" in query
    )
    ref = list(
        PaperConnector(arxiv_client=client).discover({"topic": "climate", "limit": 1})
    )[0]
    assert ref.locator.endswith("v2")
    client.fetch_by_id(ref.locator)
    assert parse_qs(urlsplit(urls[-1]).query)["id_list"] == [ref.locator]


def test_repeat_full_text_observations_do_not_manufacture_revisions():
    from src.ingestion.document_store import DocumentStore

    conn = duckdb.connect()
    try:
        store = DocumentStore(conn)

        def document(fetched, digest="same"):
            return {
                "document_id": "paper-v2",
                "source_type": "paper",
                "language": "en",
                "ingested_at": fetched,
                "title": "Research",
                "content": "Evidence",
                "metadata": {
                    "content_coverage": "full-text",
                    "version_id": "2401.00001v2",
                    "acquisition_provenance_json": json.dumps(
                        {"mode": "live-fetch", "fetched_at": fetched}
                    ),
                    "full_text_provenance_json": json.dumps(
                        {
                            "snapshot": {"digest": digest, "fetched_at": fetched},
                            "outcome": "full-text",
                        }
                    ),
                },
            }

        assert store.upsert([document(1)]).inserted == 1
        assert store.upsert([document(2)]).duplicate == 1
        changed = store.upsert([document(3, "corrected-source")])
        assert changed.changes[0]["appended"]
        assert len(store.revisions.history("paper-v2")) == 2
        assert store.get("paper-v2")["metadata"]["content_coverage"] == "full-text"
    finally:
        conn.close()


def test_paper_version_history_keeps_abstract_and_full_text_coverage():
    from src.ingestion.connectors.paper.connector import PaperConnector
    from src.ingestion.connectors.base import RawDocument, SourceRef
    from src.ingestion.document_store import DocumentStore

    class Acquirer:
        def acquire(self, url):
            return {
                "outcome": "full-text",
                "text": "Verified body evidence",
                "snapshot": {"digest": "binary-snapshot", "fetched_at": 2},
                "sections": [{"title": "Results"}],
            }

    def raw(version):
        xml = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2401.00001v{version}</id><title>Research</title><summary>Abstract only</summary><link title="pdf" href="https://arxiv.org/pdf/2401.00001v{version}"/></entry></feed>'
        return RawDocument(SourceRef("2401.00001"), xml, fetched_at=version)

    conn = duckdb.connect()
    try:
        store = DocumentStore(conn)
        first = PaperConnector().parse(raw(1))[0]
        second = PaperConnector(full_text_acquirer=Acquirer()).parse(raw(2))[0]
        assert first.document_id == second.document_id
        store.upsert([first])
        store.upsert([second])
        revisions = store.revisions.history(first.document_id)
        assert [r["payload"]["metadata"]["version_id"] for r in revisions] == [
            "2401.00001v1",
            "2401.00001v2",
        ]
        assert [r["payload"]["metadata"]["content_coverage"] for r in revisions] == [
            "abstract-only",
            "full-text",
        ]
        assert (
            len({r["payload"]["metadata"]["work_identifier"] for r in revisions}) == 1
        )
        assert store.get(first.document_id)["content"] == "Verified body evidence"
    finally:
        conn.close()
