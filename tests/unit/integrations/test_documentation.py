
import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.integrations.documentation import Context7Research


class Adapter:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def describe(self):
        return {"source_id": "context7-mcp"}

    def query(self, request, *, scopes):
        self.calls += 1
        return {
            "items": [{"value": {"content": [{"type": "text", "text": self.text}]}}],
            "provenance": {"observed_at_ms": 100, "query_hash": "query-fixture"},
        }


def test_ambiguous_libraries_and_unattested_versions():
    adapter = Adapter(
        "- Context7-compatible library ID: /unit/pint\n- Context7-compatible library ID: /pulsar/pint"
    )
    client = Context7Research(adapter)
    resolved = client.resolve("pint", "units", scopes={"operator"})
    assert (
        resolved["status"] == "selection_required" and len(resolved["library_ids"]) == 2
    )
    with pytest.raises(ValueError, match="conflicts"):
        client.query(
            "/unit/pint/v1", "units", requested_version="v2", scopes={"operator"}
        )
    assert adapter.calls == 1
    adapter.text = "No citation supplied"
    result = client.query(
        "/unit/pint", "units", requested_version="0.25.3", scopes={"operator"}
    )
    assert result["source_identity_status"] == "missing_citations"
    assert result["resolved_version"] is None and not result["source_snapshot_verified"]


def test_original_capture_replay_and_unavailable_source(tmp_path):
    url = "https://docs.example.org/pint"
    adapter = Adapter("Source: " + url + "\nSnippet: units")
    times = iter([101, 102])
    response = {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "content": "<h1>Einheiten</h1><p>Größen in Metern</p>".encode(),
    }
    client = Context7Research(
        adapter,
        transport=lambda **_: response,
        now=lambda: next(times),
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    snippets = client.query("/unit/pint", "units", scopes={"operator"})
    conn = duckdb.connect(str(tmp_path / "documentation.duckdb"))
    store = DocumentStore(conn)
    first = client.capture(
        snippets, url, store, allowed_hosts=["docs.example.org"], language="de"
    )
    second = client.capture(
        snippets, url, store, allowed_hosts=["docs.example.org"], language="de"
    )
    assert first["snapshot"]["digest"] == second["snapshot"]["digest"]
    assert second["document_result"]["duplicate"] == 1
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM source_binary_observations").fetchone()[0]
        == 2
    )
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == 1
    )
    with pytest.raises(ValueError, match="cited"):
        client.capture(
            snippets,
            "https://docs.example.org/other",
            store,
            allowed_hosts=["docs.example.org"],
            language="de",
        )
    response["status"] = 403
    with pytest.raises(ValueError, match="unavailable"):
        client.capture(
            snippets, url, store, allowed_hosts=["docs.example.org"], language="de"
        )
    conn.close()
    conn = duckdb.connect(str(tmp_path / "documentation.duckdb"))
    assert (
        conn.execute("SELECT payload FROM source_binary_blobs").fetchone()[0]
        == response["content"]
    )
    conn.close()
