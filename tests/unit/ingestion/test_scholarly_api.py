import json
from pathlib import Path
import pytest
from src.ingestion.scholarly_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import validate_source_pack, SourcePackError


@pytest.mark.parametrize("name", ["crossref", "openalex"])
def test_native_cursor_pages_and_restart(name):
    source = next(
        s
        for s in validate_source_pack(
            json.loads(Path("config/source_packs/research.json").read_text())
        )["sources"]
        if s["source_id"] == name + "-works"
    )
    item = (
        {
            "DOI": "10.1234/ABC",
            "title": ["Title"],
            "author": [{"given": "A", "family": "B"}],
            "published": {"date-parts": [[2024, 2]]},
        }
        if name == "crossref"
        else {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1234/ABC",
            "title": "Title",
            "abstract_inverted_index": {"An": [0], "abstract": [1]},
            "authorships": [{"author": {"display_name": "A B"}}],
        }
    )
    calls = []

    def transport(**kw):
        calls.append(kw)
        cursor = kw["params"]["cursor"]
        values = [item] if cursor in ("*", "second") else []
        payload = (
            {
                "message": {
                    "items": values,
                    "next-cursor": "second" if cursor == "*" else "end",
                }
            }
            if name == "crossref"
            else {
                "results": values,
                "meta": {"next_cursor": "second" if cursor == "*" else "end"},
            }
        )
        return {"content": json.dumps(payload)}

    adapter = HTTPSPageAdapter(source, transport=transport, secret="private-key")
    request = {"operation": "search", "parameters": {"query": "topic"}, "limit": 1}
    first = adapter.fetch_page(request, cursor=None)
    second = HTTPSPageAdapter(
        source, transport=transport, secret="private-key"
    ).fetch_page(request, cursor=first.next_cursor)
    assert first.records[0]["id"] == second.records[0]["id"]
    assert first.records[0]["doi"] == "10.1234/abc"
    assert first.records[0]["authors"] == ["A B"]
    assert adapter.fetch_page(request, cursor=second.next_cursor).records == ()
    assert "Authorization" not in calls[0]["headers"]
    assert "private-key" not in json.dumps(first.receipt)
    assert "limit" not in calls[0]["params"]
    assert first.records[0]["content_representation"] == (
        "title-only" if name == "crossref" else "plain-text-abstract"
    )
    assert first.records[0]["content"] in ("Title", "An abstract")


def test_native_filters_bounds_and_credentials():
    p = parameters(
        "crossref",
        {
            "parameters": {"doi": "https://doi.org/10.1/example", "rows": 9000},
            "from_ms": 0,
        },
        cursor="resume",
        limit=2,
        contact="contact@example.org",
        secret="unused",
    )
    assert p == {
        "filter": "doi:10.1/example,from-pub-date:1970-01-01",
        "cursor": "resume",
        "rows": 2,
        "mailto": "contact@example.org",
    }
    p = parameters(
        "openalex",
        {"parameters": {"author": "A1"}, "to_ms": 0},
        cursor=None,
        limit=1000,
        secret="private",
    )
    assert (
        p["per_page"] == 200
        and p["filter"] == "authorships.author.id:A1,to_publication_date:1970-01-01"
    )
    assert p["api_key"] == "private"


@pytest.mark.parametrize("name", ["crossref", "openalex"])
def test_malformed_envelopes_fail(name):
    with pytest.raises(ValueError):
        records(name, {"data": []}, cursor=None, limit=1)
    payload = (
        {"message": {"items": [{}, {}]}}
        if name == "crossref"
        else {"results": [{}, {}]}
    )
    with pytest.raises(ValueError):
        records(name, payload, cursor=None, limit=1)


@pytest.mark.parametrize("name", ["crossref", "openalex"])
def test_native_runtime_reopens_durable_cursor_without_duplicate_documents(
    tmp_path, name
):
    import duckdb
    from src.ingestion.source_packs import SourcePackStore
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    raw_manifest = json.loads(Path("config/source_packs/research.json").read_text())
    raw_manifest["defaults"]["budgets"]["max_results"] = 2
    manifest = validate_source_pack(raw_manifest)
    source = next(s for s in manifest["sources"] if s["source_id"] == name + "-works")
    calls = []

    def transport(**kw):
        cursor = kw["params"]["cursor"]
        calls.append(cursor)
        ids = [1, 2] if cursor == "*" else [2, 3] if cursor == "second" else []
        items = (
            [{"DOI": f"10.1234/{i}", "title": [f"Paper {i}"]} for i in ids]
            if name == "crossref"
            else [
                {"id": f"https://openalex.org/W{i}", "title": f"Paper {i}"} for i in ids
            ]
        )
        next_cursor = "second" if cursor == "*" else "end"
        return {
            "content": json.dumps(
                {"message": {"items": items, "next-cursor": next_cursor}}
                if name == "crossref"
                else {"results": items, "meta": {"next_cursor": next_cursor}}
            )
        }

    value = {
        "pack_id": manifest["pack_id"],
        "run_key": "native-resume",
        "operation": "search",
        "source_ids": [source["source_id"]],
        "required_sources": [source["source_id"]],
        "max_pages": 5,
        "max_results": 10,
        "parameters": {"query": "research"},
    }

    # Runtime page size follows the source declaration; use two-item fixture pages.
    def crash(_source, page):
        if page == 1:
            raise RuntimeError("interrupted after durable page")

    db = tmp_path / "resume.duckdb"
    conn = duckdb.connect(str(db))
    SourcePackStore(conn).install(manifest, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    runtime.accept_license(
        manifest["pack_id"], source["source_id"], principal_id="operator"
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        runtime.run(
            value,
            principal_id="operator",
            adapters={
                source["source_id"]: HTTPSPageAdapter(source, transport=transport)
            },
            dns_resolver=lambda _: ["8.8.8.8"],
            fault=crash,
        )
    conn.close()
    conn = duckdb.connect(str(db))
    try:
        result = SourcePackRuntime(conn).run(
            value,
            principal_id="operator",
            adapters={
                source["source_id"]: HTTPSPageAdapter(source, transport=transport)
            },
            dns_resolver=lambda _: ["8.8.8.8"],
        )
        assert result["status"] == "complete"
        assert calls == ["*", "second", "end"]
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (3,)
    finally:
        conn.close()
