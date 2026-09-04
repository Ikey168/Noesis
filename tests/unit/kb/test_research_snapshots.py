from __future__ import annotations

import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.artifacts import ArtifactGraph
from src.kb.derived_revisions import DerivedRevisionStore
from src.kb.research_snapshots import ResearchSnapshotError, ResearchSnapshotStore
from src.kb.unified_query import validate_query_request

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {"knowledge:snapshot:read", "knowledge:snapshot:write", "knowledge:read"}


class Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def setup_store():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE knowledge_maintenance_generations (generation_id TEXT,pack_id TEXT,"
        "generation BIGINT,source_watermark BIGINT,workflow_run_id TEXT,workflow_watermark BIGINT,"
        "artifact_watermark BIGINT,status TEXT,receipt_hash TEXT,receipt_json TEXT,committed_at_ms BIGINT)"
    )
    conn.execute(
        "INSERT INTO knowledge_maintenance_generations VALUES "
        "('g1','pack-a',2,7,'w1',3,4,'complete','hash','{}',100)"
    )
    derived = DerivedRevisionStore(conn)
    receipt = derived.apply_generation(
        "ns-a",
        7,
        [
            {
                "object_type": "claim",
                "content": {"statement": "Result"},
                "document_id": "d1",
                "source_revision_id": "r1",
                "producer": {"name": "fixture", "version": "1"},
            }
        ],
        [{"document_id": "d1", "revision_id": "r1", "change_kind": "added"}],
        now_ms=100,
    )
    derived.publish_generation("ns-a", receipt["generation"])
    ArtifactGraph(conn)
    conn.execute(
        "INSERT INTO knowledge_artifact_watermarks VALUES ('ns-a',5,'build',100)"
    )
    clock = Clock()
    return conn, clock, ResearchSnapshotStore(conn, now=clock)


def selection(**extra):
    return {
        "packs": ["pack-a"],
        "namespaces": ["ns-a"],
        "domains": ["research"],
        "federated": [
            {
                "source_id": "remote-a",
                "consistency": "snapshot",
                "generation": "42",
                "capability_hash": "cap",
            }
        ],
        **extra,
    }


def test_begin_pins_vector_and_binds_query_hash():
    conn, _, store = setup_store()
    begun = store.begin(selection(), principal_id="alice", scopes=SCOPES, ttl_ms=5_000)
    assert begun["vector"]["packs"]["pack-a"]["source_watermark"] == 7
    assert begun["vector"]["namespaces"]["ns-a"] == {
        "derived_generation": 7,
        "change_hash": begun["vector"]["namespaces"]["ns-a"]["change_hash"],
        "artifact_watermark": 5,
    }
    inspected = store.inspect(begun["token"], principal_id="alice", scopes=SCOPES)
    assert "token" not in inspected
    assert (
        len(store.pins(begun["token"], principal_id="alice", scopes=SCOPES)["pins"])
        == 3
    )
    request = {
        "query": "result",
        "scope": {"domains": ["research"], "namespaces": ["ns-a"]},
    }
    bound = store.bind_query(
        begun["token"], request, principal_id="alice", scopes=SCOPES
    )
    normalized = validate_query_request(bound)
    assert normalized["snapshot"]["vector_hash"] == begun["vector_hash"]
    conn.close()


def test_scope_owner_expiry_renewal_and_close_enforcement():
    conn, clock, store = setup_store()
    begun = store.begin(
        selection(),
        principal_id="alice",
        scopes=SCOPES,
        ttl_ms=2_000,
        maximum_lifetime_ms=4_000,
    )
    with pytest.raises(ResearchSnapshotError, match="does not exist"):
        store.inspect(begun["token"], principal_id="bob", scopes=SCOPES)
    with pytest.raises(ResearchSnapshotError, match="exceed"):
        store.bind_query(
            begun["token"],
            {"query": "x", "scope": {"domains": ["technical"]}},
            principal_id="alice",
            scopes=SCOPES,
        )
    clock.value = 2_000
    renewed = store.renew(
        begun["token"], principal_id="alice", scopes=SCOPES, ttl_ms=9_000
    )
    assert renewed["expires_at_ms"] == 5_000
    clock.value = 5_000
    with pytest.raises(ResearchSnapshotError, match="expired"):
        store.bind_query(
            begun["token"],
            {"query": "x", "scope": {"domains": ["research"]}},
            principal_id="alice",
            scopes=SCOPES,
        )
    closed = store.close(begun["token"], principal_id="alice", scopes=SCOPES)
    assert closed["status"] == "closed"
    conn.close()


def test_unavailable_and_degraded_federated_generation_handling():
    conn, _, store = setup_store()
    with pytest.raises(ResearchSnapshotError, match="cannot be pinned"):
        store.begin(
            selection(
                packs=["missing"],
                federated=[{"source_id": "live", "consistency": "eventual"}],
            ),
            principal_id="alice",
            scopes=SCOPES,
        )
    degraded = store.begin(
        selection(
            packs=["missing"],
            federated=[{"source_id": "live", "consistency": "eventual"}],
            allow_degraded=True,
        ),
        principal_id="alice",
        scopes=SCOPES,
    )
    assert {item["kind"] for item in degraded["omissions"]} == {"federated", "pack"}
    conn.close()


def test_token_schema_health_and_tamper_rejection():
    conn, _, store = setup_store()
    begun = store.begin(selection(), principal_id="alice", scopes=SCOPES, ttl_ms=2_000)
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-research-snapshot-token-v1.json"
        ).read_text()
    )
    jsonschema.validate(begun, schema)
    session_schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-research-snapshot-v1.json"
        ).read_text()
    )
    jsonschema.validate(
        store.inspect(begun["token"], principal_id="alice", scopes=SCOPES),
        session_schema,
    )
    with pytest.raises(ResearchSnapshotError, match="does not exist"):
        store.inspect(begun["token"] + "x", principal_id="alice", scopes=SCOPES)
    assert store.health()["active_pins"] == 3
    conn.close()


def test_six_domain_scope_is_preserved_in_snapshot_bound_queries():
    conn, _, store = setup_store()
    domains = ["research", "political", "economic", "osint", "technical", "scientific"]
    begun = store.begin(
        selection(domains=domains), principal_id="alice", scopes=SCOPES, ttl_ms=2_000
    )
    bound = store.bind_query(
        begun["token"],
        {"query": "compare evidence", "scope": {"domains": domains}},
        principal_id="alice",
        scopes=SCOPES,
    )
    normalized = validate_query_request(bound)
    assert normalized["scope"]["domains"] == sorted(domains)
    assert normalized["snapshot"]["session_id"] == begun["session_id"]
    conn.close()
