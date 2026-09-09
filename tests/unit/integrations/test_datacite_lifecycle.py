"""DataCite native requests through collection, commit, replay and revision reads."""

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.datacite_api import parameters
from src.ingestion.document_store import DocumentStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter, SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack


def test_native_operation_queries_preserve_filters_and_update_bounds():
    result = parameters(
        {
            "operation": "author",
            "parameters": {"query": 'Müller, "Anna"', "client-id": "tib"},
            "from_ms": 0,
            "to_ms": 1000,
        },
        cursor="next",
        limit=2,
    )
    assert result["client-id"] == "tib"
    assert result["page[cursor]"] == "next"
    assert (
        result["query"]
        == 'creators.name:"Müller, \\"Anna\\"" AND updated:[1970-01-01T00:00:00.000Z TO 1970-01-01T00:00:01.000Z]'
    )
    assert (
        parameters(
            {
                "operation": "doi",
                "parameters": {"query": "https://doi.org/10.1234/ABC"},
            },
            cursor=None,
            limit=1,
        )["query"]
        == 'doi:"10.1234/abc"'
    )
    assert (
        parameters(
            {"operation": "version", "parameters": {"query": "2"}}, cursor=None, limit=1
        )["query"]
        == 'version:"2"'
    )


def test_native_multipage_commit_replay_and_historical_relationships():
    conn = duckdb.connect()
    manifest = validate_source_pack(
        json.loads(Path("config/source_packs/scientific.json").read_text())
    )
    source = next(s for s in manifest["sources"] if s["source_id"] == "datacite-dois")
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=1
    )
    clock = iter(range(1000, 100000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _: None)
    runtime.accept_license(
        manifest["pack_id"], source["source_id"], principal_id="operator"
    )
    native = json.loads(
        Path("tests/fixtures/integrations/datacite-native.json").read_text()
    )
    # Real-shaped provider records with controlled version transitions and absent targets.
    native = copy.deepcopy(native)
    native["data"][0]["attributes"]["version"] = "1"
    native["data"][0]["attributes"]["relatedIdentifiers"] = [
        {
            "relatedIdentifier": "10.1234/uncollected",
            "relatedIdentifierType": "DOI",
            "relationType": "IsVersionOf",
        }
    ]
    calls = []

    def transport(**kwargs):
        cursor = kwargs["params"]["page[cursor]"]
        calls.append(cursor)
        index = 0 if cursor == "1" else 1
        page = {"data": [native["data"][index]], "links": {}}
        if index == 0:
            page["links"]["next"] = (
                "https://api.datacite.org/dois?page%5Bcursor%5D=second"
            )
        return {"status": 200, "content": json.dumps(page)}

    def run(key):
        return runtime.run(
            {
                "pack_id": manifest["pack_id"],
                "run_key": key,
                "operation": "topic",
                "parameters": {"query": "Berlin"},
                "source_ids": [source["source_id"]],
                "required_sources": [source["source_id"]],
                "max_pages": 5,
                "max_results": 100,
                "max_bytes": 1000000,
                "timeout_ms": 10000,
            },
            principal_id="operator",
            adapters={
                source["source_id"]: HTTPSPageAdapter(source, transport=transport)
            },
            dns_resolver=lambda _: ["8.8.8.8"],
        )

    run("initial")
    assert calls == ["1", "second"]
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
    document_id = conn.execute(
        "SELECT document_id FROM documents WHERE url=?",
        ["https://doi.org/" + native["data"][0]["attributes"]["doi"].lower()],
    ).fetchone()[0]
    store = DocumentStore(conn)
    first = store.related_resources(document_id)
    assert first["links"][0]["predicate"] == "IsVersionOf"
    assert first["links"][0]["target_identifier"] == "10.1234/uncollected"
    before = conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[
        0
    ]
    run("replay")
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == before
    )
    native["data"][0]["attributes"]["version"] = "2"
    native["data"][0]["attributes"]["relatedIdentifiers"] = []
    run("updated")
    assert store.related_resources(document_id)["links"] == []
    assert store.related_resources(document_id, revision=first["revision"]) == first
    assert (
        conn.execute(
            "SELECT count(*) FROM document_revision_records WHERE committed_watermark IS NULL"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_part_relations_preserve_direction_and_non_doi_identifiers():
    from src.ingestion.datacite_api import related_resources

    links = related_resources(
        "10.1234/source",
        {
            "relatedIdentifiers": [
                {
                    "relatedIdentifier": "https://example.org/dataset",
                    "relatedIdentifierType": "URL",
                    "relationType": "HasPart",
                },
                {
                    "relatedIdentifier": "10.1234/PARENT",
                    "relatedIdentifierType": "DOI",
                    "relationType": "IsPartOf",
                },
            ]
        },
    )
    assert [(link["predicate"], link["target_identifier_type"]) for link in links] == [
        ("HasPart", "URL"),
        ("IsPartOf", "DOI"),
    ]
    assert all(
        link["source_identifier"] == "10.1234/source" and link["provider"] == "datacite"
        for link in links
    )
    assert links[1]["target_identifier"] == "10.1234/parent"
