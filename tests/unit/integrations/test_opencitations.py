import copy
import json
from pathlib import Path

import pytest

from src.ingestion.opencitations import OpenCitationsClient
from src.integrations.common import digest
from src.knowledge_graph.foundation import KnowledgeGraphStore, RelationType


def fixture():
    return json.loads(
        Path("tests/fixtures/integrations/opencitations-native.json").read_text()
    )


def test_native_snapshot_resumes_and_replays_without_duplicate_edges():
    snapshot = fixture()
    client = OpenCitationsClient()
    store = KnowledgeGraphStore()
    first = client.ingest_snapshot(snapshot, store, page_size=3)
    assert first["imported"] == 3
    assert client.ingest_snapshot(snapshot, store, page_size=3)["imported"] == 0
    cursor = first["next_cursor"]
    while cursor:
        cursor = client.ingest_snapshot(snapshot, store, cursor=cursor, page_size=10)[
            "next_cursor"
        ]
    edges = store.triples(predicate=RelationType.CITES)
    assert len(edges) == len({(r["citing"], r["cited"]) for r in snapshot["records"]})
    assert all(len(store.provenance_for(edge)) == 1 for edge in edges)
    assert all(
        edge.properties["opencitations"]["snapshot_sha256"] == snapshot["sha256"]
        for edge in edges
    )
    assert store.neighbors(snapshot["identifier"])
    changed = copy.deepcopy(snapshot)
    changed["observed_at_ms"] += 1
    changed["sha256"] = digest({k: v for k, v in changed.items() if k != "sha256"})
    with pytest.raises(ValueError, match="another snapshot"):
        client.ingest_snapshot(changed, store, cursor=first["next_cursor"])
    client.ingest_snapshot(changed, store)
    assert store.triple_count == len(edges)


def test_incoming_direction_errors_and_missing_identifiers():
    snapshot = fixture()
    record = snapshot["records"][0]
    cited_doi = next(i for i in record["cited"].split() if i.startswith("doi:"))
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"content": json.dumps([record, record])}

    client = OpenCitationsClient(transport=transport)
    incoming = client.snapshot(cited_doi, direction="citations", observed_at_ms=1)
    assert "/citations/" in calls[0]["url"] and calls[0]["params"] == {}
    assert client.ingest_snapshot(incoming, KnowledgeGraphStore())["imported"] == 1
    with pytest.raises(ValueError):
        client.snapshot("no identifier")
    record["cited"] = ""
    with pytest.raises(ValueError, match="lacks"):
        client.snapshot(snapshot["identifier"])
    failed = OpenCitationsClient(transport=lambda **_: {"status": 429, "content": b""})
    with pytest.raises(ValueError, match="failed"):
        failed.snapshot(snapshot["identifier"])


def test_durable_capture_resume_mirrored_edge_and_traversal(tmp_path):
    import duckdb

    from src.ingestion.connectors.paper.citation_graph import build_citation_graph
    from src.ingestion.connectors.paper.models import PaperMetadata
    from src.ingestion.connectors.paper.references import SemanticScholarReferences
    from src.ingestion.opencitations import CitationAcquisitionStore, traverse_citations
    from src.knowledge_graph.foundation import DuckDBKnowledgeGraphStore

    snapshot = fixture()
    client = OpenCitationsClient(
        transport=lambda **_: {"content": json.dumps(snapshot["records"])}
    )
    db = tmp_path / "citation.duckdb"
    conn = duckdb.connect(str(db))
    acquisition = CitationAcquisitionStore(conn)
    first = acquisition.acquire(snapshot["identifier"], client=client, page_size=3)
    conn.close()
    conn = duckdb.connect(str(db))
    acquisition = CitationAcquisitionStore(conn)
    cursor = first["next_cursor"]
    offline = OpenCitationsClient(
        transport=lambda **_: pytest.fail("resume must not fetch")
    )
    while cursor:
        cursor = acquisition.acquire(
            snapshot["identifier"],
            client=offline,
            snapshot_sha256=first["snapshot_sha256"],
            cursor=cursor,
            page_size=10,
        )["next_cursor"]
    graph = DuckDBKnowledgeGraphStore(connection=conn)
    edge = graph.triples(predicate=RelationType.CITES)[0]
    paper = PaperMetadata(
        title="Authored overlap fixture", doi=edge.subject.removeprefix("doi:")
    )
    baseline = SemanticScholarReferences(
        http_get=lambda _: json.dumps(
            {
                "data": [
                    {
                        "citedPaper": {
                            "title": "Authored reference fixture",
                            "externalIds": {
                                "DOI": edge.object.removeprefix("doi:").upper()
                            },
                        }
                    }
                ]
            }
        ).encode()
    )
    paper.references = baseline.references_for(paper)
    build_citation_graph(graph, paper)
    result = traverse_citations(
        conn, snapshot["identifier"], direction="references", limit=100
    )
    assert result["edge_count"] == len(snapshot["records"])
    mirrored = next(
        e
        for e in result["edges"]
        if e["to"] == edge.object and e["from"] == edge.subject
    )
    assert len(mirrored["provenance"]) == 2
    assert mirrored["independent_evidence_count"] is None
    assert any(
        p["observed_at_ms"] == first["observed_at_ms"] for p in mirrored["provenance"]
    )
    assert (
        acquisition.acquire(
            snapshot["identifier"],
            client=offline,
            snapshot_sha256=first["snapshot_sha256"],
        )["imported"]
        == 0
    )
    assert traverse_citations(conn, snapshot["identifier"], limit=1)["bounded"]
    with pytest.raises(ValueError, match="another traversal"):
        acquisition.acquire(
            snapshot["identifier"],
            snapshot_sha256=first["snapshot_sha256"],
            direction="citations",
        )
    conn.close()


def test_failed_page_rolls_back_graph_and_snapshot(tmp_path):
    import duckdb

    from src.ingestion.opencitations import CitationAcquisitionStore
    from src.knowledge_graph.foundation import (
        DuckDBKnowledgeGraphStore,
        EntityType,
        Node,
    )

    snapshot = fixture()
    # Force a real graph type collision after at least one successful edge.
    target = next(
        v for v in snapshot["records"][1]["cited"].split() if v.startswith("doi:")
    )
    conn = duckdb.connect(str(tmp_path / "atomic.duckdb"))
    graph = DuckDBKnowledgeGraphStore(connection=conn)
    graph.add_node(Node(EntityType.PERSON, "Collision fixture", node_id=target))
    client = OpenCitationsClient(
        transport=lambda **_: {"content": json.dumps(snapshot["records"])}
    )
    with pytest.raises(ValueError):
        CitationAcquisitionStore(conn).acquire(
            snapshot["identifier"], client=client, page_size=3
        )
    assert conn.execute("SELECT count(*) FROM kg_triples").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM citation_provider_snapshots").fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT count(*) FROM kg_nodes").fetchone()[0] == 1
    conn.close()
