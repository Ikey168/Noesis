import copy
import json

import duckdb
import pytest

from src.ingestion.orcid import acquire_author, normalize_record, validate_orcid
from src.kb.entity_history import EntityHistoryStore

ORCID = "0000-0002-1825-0097"


def record():
    return {
        "orcid-identifier": {"path": ORCID},
        "person": {
            "name": {
                "visibility": "PUBLIC",
                "given-names": {"value": "Jörg"},
                "family-name": {"value": "Müller"},
            }
        },
        "activities-summary": {
            "works": {
                "group": [
                    {
                        "work-summary": [
                            {
                                "put-code": 1,
                                "visibility": "PUBLIC",
                                "title": {"title": {"value": "Forschung in Berlin"}},
                                "external-ids": {
                                    "external-id": [
                                        {
                                            "external-id-type": "doi",
                                            "external-id-value": "10.1234/example",
                                        }
                                    ]
                                },
                                "source": {"source-name": {"value": "University"}},
                            },
                            {
                                "put-code": 2,
                                "visibility": "PRIVATE",
                                "title": {"title": {"value": "Private project"}},
                            },
                        ]
                    }
                ]
            },
            "employments": {
                "affiliation-group": [
                    {
                        "summaries": [
                            {
                                "employment-summary": {
                                    "put-code": 3,
                                    "visibility": "PUBLIC",
                                    "organization": {"name": "Berliner Universität"},
                                    "start-date": {"year": {"value": "2024"}},
                                    "source": {"source-name": {"value": "Researcher"}},
                                }
                            }
                        ]
                    }
                ]
            },
        },
    }


def test_public_mapping_and_identifier_validation():
    assert validate_orcid("https://orcid.org/" + ORCID) == ORCID
    with pytest.raises(ValueError, match="checksum"):
        validate_orcid("0000-0002-1825-0098")
    raw = record()
    before = copy.deepcopy(raw)
    value = normalize_record(raw, ORCID)
    assert raw == before and len(value["works"]) == 1
    assert "Private project" not in json.dumps(value)
    assert value["affiliations"][0]["organization"]["name"] == "Berliner Universität"
    with pytest.raises(ValueError, match="limit"):
        normalize_record(raw, ORCID, max_items=1)
    raw["person"]["name"]["visibility"] = "PRIVATE"
    assert normalize_record(raw, ORCID)["name_status"] == "unavailable"


def test_same_name_candidates_go_to_existing_inbox_and_replay(tmp_path):
    path = str(tmp_path / "authors.duckdb")
    conn = duckdb.connect(path)
    auth = {"principal_id": "operator", "scopes": {"operator"}}
    history = EntityHistoryStore(conn)
    for identity in ("person-a", "person-b"):
        history.register_entity("research", identity, aliases=["Jörg Müller"], **auth)
    calls = []
    raw = record()

    def transport(**kw):
        calls.append(kw)
        return {"content": json.dumps(raw)}

    opts = {
        "request_id": "first",
        "access_token": "private-token",
        "namespace": "research",
        "language": "de",
        "candidates": [{"entity_id": "person-a"}, {"entity_id": "person-b"}],
        "transport": transport,
        **auth,
    }
    result = acquire_author(conn, ORCID, **opts)
    assert result["match_status"] == "ambiguous" and result["review_task_id"]
    assert conn.execute("SELECT count(*) FROM review_inbox_tasks").fetchone() == (1,)
    assert "private-token" not in json.dumps(result)
    assert conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone() == (
        0,
    )
    conn.close()
    conn = duckdb.connect(path)
    try:
        assert acquire_author(conn, ORCID, **opts) == result
        assert len(calls) == 1
        raw["activities-summary"]["employments"]["affiliation-group"][0]["summaries"][
            0
        ]["employment-summary"]["organization"]["name"] = "Neue Universität"
        revised = acquire_author(conn, ORCID, **{**opts, "request_id": "second"})
        assert revised["document_id"] == result["document_id"]
        assert conn.execute(
            "SELECT count(*) FROM document_revision_records"
        ).fetchone() == (2,)
    finally:
        conn.close()


def test_explicit_identifier_match_and_unavailable():
    conn = duckdb.connect()
    auth = {"principal_id": "operator", "scopes": {"operator"}}
    EntityHistoryStore(conn).register_entity("research", "person-a", **auth)
    opts = {
        "request_id": "known",
        "access_token": "private",
        "namespace": "research",
        **auth,
    }
    result = acquire_author(
        conn,
        ORCID,
        candidates=[{"entity_id": "person-a", "orcid": ORCID}],
        transport=lambda **_: {"content": json.dumps(record())},
        **opts,
    )
    assert (
        result["match_status"] == "identifier_confirmed"
        and "review_task_id" not in result
    )
    assert (
        acquire_author(
            conn,
            ORCID,
            transport=lambda **_: {"status": 403},
            **{**opts, "request_id": "denied"},
        )["status"]
        == "unavailable"
    )
    with pytest.raises(PermissionError):
        acquire_author(conn, ORCID, **{**opts, "scopes": set()})
    conn.close()
