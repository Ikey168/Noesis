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
