import copy
import json

import duckdb
import pytest

from src.ingestion.wikidata import acquire_entity, normalize_entity


def entity(revision=12, rank="normal"):
    return {
        "entities": {
            "Q64": {
                "id": "Q64",
                "lastrevid": revision,
                "labels": {"de": {"value": "Berlin"}, "en": {"value": "Berlin"}},
                "aliases": {"de": [{"value": "Bundeshauptstadt Berlin"}]},
                "claims": {
                    "P17": [
                        {
                            "id": "q64$statement",
                            "rank": rank,
                            "mainsnak": {
                                "property": "P17",
                                "snaktype": "value",
                                "datavalue": {"value": {"id": "Q183"}},
                            },
                            "qualifiers": {
                                "P580": [
                                    {
                                        "snaktype": "value",
                                        "datavalue": {
                                            "value": {"time": "+1990-10-03T00:00:00Z"}
                                        },
                                    }
                                ]
                            },
                            "references": [
                                {
                                    "hash": "reference",
                                    "snaks": {
                                        "P854": [
                                            {
                                                "datavalue": {
                                                    "value": "https://berlin.example/source"
                                                }
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                        {
                            "id": "Q64$conflicting",
                            "rank": "deprecated",
                            "mainsnak": {"property": "P17", "snaktype": "somevalue"},
                        },
                    ]
                },
            }
        }
    }


def test_statement_references_qualifiers_ranks_and_aliases():
    raw = entity()
    original = copy.deepcopy(raw)
    result = normalize_entity(raw, "Q64", properties=["P17"], languages=["de", "en"])
    assert raw == original
    assert [s["reference_status"] for s in result["statements"]] == [
        "referenced",
        "unreferenced",
    ]
    assert (
        result["statements"][0]["qualifiers"]
        == raw["entities"]["Q64"]["claims"]["P17"][0]["qualifiers"]
    )
    assert result["statements"][1]["rank"] == "deprecated"
    assert result["aliases"]["de"] == ["Bundeshauptstadt Berlin"]
    with pytest.raises(ValueError, match="limit"):
        normalize_entity(
            raw, "Q64", properties=["P17"], languages=["de"], max_statements=1
        )


def test_durable_replay_and_changed_statement(tmp_path):
    calls = []
    raw = entity()

    def fetch(**kw):
        calls.append(kw)
        return {"content": json.dumps(raw)}

    path = str(tmp_path / "entities.duckdb")
    conn = duckdb.connect(path)
    options = {
        "properties": ["P17"],
        "request_id": "first",
        "transport": fetch,
        "candidate_entity_ids": ["local-berlin-city", "local-berlin-state"],
    }
    result = acquire_entity(conn, "Q64", **options)
    assert (
        result["status"] == "available" and result["local_match_status"] == "ambiguous"
    )
    assert result["canonical_update"] == "review_required"
    conn.close()
    conn = duckdb.connect(path)
    try:
        assert acquire_entity(conn, "Q64", **options) == result
        assert len(calls) == 1
        raw = entity(revision=13, rank="deprecated")
        revised = acquire_entity(conn, "Q64", **{**options, "request_id": "second"})
        assert revised["document_id"] == result["document_id"]
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (1,)
        assert (
            '"revision": 13'
            in conn.execute("SELECT content FROM documents").fetchone()[0]
        )
        assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (
            2,
        )
    finally:
        conn.close()


def test_missing_entity_revision_mismatch_and_failure():
    conn = duckdb.connect()
    result = acquire_entity(
        conn,
        "Q64",
        properties=["P17"],
        request_id="missing",
        transport=lambda **_: {"status": 404},
    )
    assert result["status"] == "unavailable"
    assert conn.execute("SELECT count(*) FROM documents").fetchone() == (0,)
    with pytest.raises(ValueError, match="different revision"):
        acquire_entity(
            conn,
            "Q64",
            properties=["P17"],
            revision=11,
            request_id="mismatch",
            transport=lambda **_: {"content": json.dumps(entity())},
        )
    with pytest.raises(ValueError, match="429"):
        acquire_entity(
            conn,
            "Q64",
            properties=["P17"],
            request_id="limited",
            transport=lambda **_: {"status": 429},
        )
    conn.close()
